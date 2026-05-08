from __future__ import annotations

import json
import asyncio
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_badge_api.core.permissions import PermissionScope
from smart_badge_api.db.base import Base
from smart_badge_api.db.models import Recording, Staff, VisitOrder, WecomTenant
from smart_badge_api.schemas.matching import MatchEvidenceOut
from smart_badge_api.visit_order_matching import (
    _apply_mutual_exclusion_orders,
    _build_identity_conflicts_for_candidate,
    _build_order_shortlist_facts,
    _candidate_order_to_out,
    _companion_visit_signal,
    _department_assistant_codes_from_config,
    _merge_llm_order_result,
    _customer_name_match_signal,
    _extract_addressed_identity_signals,
    _extract_payload_demographics,
    _extract_companion_customer_codes,
    _extract_structured_demands,
    _extract_order_stage_signals,
    _extract_recording_stage_signals,
    _finalize_candidate,
    _find_companion_orders,
    _heuristic_confidence,
    _recording_file_lookup_keys,
    _shortlist_orders_for_recording,
    _time_proximity_score,
    _role_time_window_score,
    _score_order_for_recording,
    _score_recording_for_order,
    _OrderCandidate,
    analyze_recording_visit_order_match,
)


def test_recording_file_lookup_keys_include_legacy_day_only_archive_variant() -> None:
    keys = _recording_file_lookup_keys("0324_130246.mp3")

    assert "0324_130246.mp3" in keys
    assert "0324_130246" in keys
    assert "24_130246.mp3" in keys
    assert "24_130246" in keys


def test_department_assistant_config_maps_staff_to_departments() -> None:
    staff = Staff(id="staff_1", name="科助A", external_account="86000995", hospital_code="6501")
    config = {
        "enabled": True,
        "departments": [
            {"department_code": "JGKS03", "assistant_staff_ids": ["staff_1"]},
            {"department_code": "JGKS04", "assistant_staff_ids": ["86000995"]},
            {"department_code": "JGKS02", "assistant_staff_ids": ["other_staff"]},
        ],
    }

    assert _department_assistant_codes_from_config(config, staff) == ["JGKS03", "JGKS04"]


def test_department_assistant_department_match_enters_order_shortlist() -> None:
    staff = Staff(id="staff_1", name="科助A", external_account="86000995", hospital_code="6501")
    recording = Recording(
        file_name="dept-assistant.mp3",
        file_path="/tmp/dept-assistant.mp3",
        status="uploaded",
        created_at=datetime(2026, 4, 18, 10, 15),
    )
    recording.staff = staff
    department_order = VisitOrder(
        id="vo_department",
        dzdh="DZ1001",
        jgbm="6501",
        sjrq="2026-04-18",
        jgks="JGKS03",
        jgks_txt="外科",
        advxc="other_staff",
    )
    other_department_order = VisitOrder(
        id="vo_other_department",
        dzdh="DZ1002",
        jgbm="6501",
        sjrq="2026-04-18",
        jgks="JGKS02",
        jgks_txt="皮肤科",
        advxc="other_staff",
    )

    shortlisted = _shortlist_orders_for_recording(
        recording,
        [department_order, other_department_order],
        {"record_date": "2026-04-18", "start_seconds": 10 * 3600 + 15 * 60},
        staff,
        staff_position_text="科室助理 JGKS03 外科",
    )

    assert [order.id for order, _facts in shortlisted] == ["vo_department"]
    assert shortlisted[0][1]["department_assistant_match"] is True


def test_recording_match_includes_configured_department_assistant_orders_in_staff_scope() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                tenant = WecomTenant(
                    id="tenant_csyamei",
                    name="长沙雅美",
                    default_hospital_code="6501",
                    is_active=True,
                    department_assistant_match_config={
                        "enabled": True,
                        "departments": [
                            {
                                "department_code": "JGKS03",
                                "department_name": "外科",
                                "assistant_staff_ids": ["staff_dept_assistant"],
                            }
                        ],
                    },
                )
                staff = Staff(
                    id="staff_dept_assistant",
                    name="科室助理A",
                    external_account="86000995",
                    hospital_code="6501",
                    role="consultant",
                    permission_role="staff",
                )
                recording = Recording(
                    id="rec_dept_assistant",
                    staff_id=staff.id,
                    file_name="20260418_101500.mp3",
                    file_path="/tmp/20260418_101500.mp3",
                    status="completed",
                    transcript_text="今天先看看眼部基础，稍后医生再面诊设计方案。",
                    created_at=datetime(2026, 4, 18, 10, 15, tzinfo=UTC),
                )
                department_order = VisitOrder(
                    id="vo_department",
                    dzdh="DZ1001",
                    dzseg="110",
                    jgbm="6501",
                    crtdt="2026-04-18",
                    sjrq=None,
                    advxc="81000001",
                    fzuer="81000001",
                    yyuer="82000001",
                    jgks="JGKS03",
                    jgks_txt="外科",
                    ninam="科室客户",
                    remark_dz="眼部面诊",
                )
                other_department_order = VisitOrder(
                    id="vo_other_department",
                    dzdh="DZ1002",
                    dzseg="110",
                    jgbm="6501",
                    crtdt="2026-04-18",
                    sjrq=None,
                    advxc="81000001",
                    jgks="JGKS02",
                    jgks_txt="皮肤科",
                    ninam="其他科室客户",
                )

                db.add_all([tenant, staff, recording, department_order, other_department_order])
                await db.commit()

            async with session_factory() as db:
                result = await analyze_recording_visit_order_match(
                    db,
                    "rec_dept_assistant",
                    apply_auto=False,
                    use_llm=False,
                    scope=PermissionScope(role="staff", staff_id="staff_dept_assistant", hospital_code="6501"),
                )

                assert result is not None
                assert result.record_date == "2026-04-18"
                assert [candidate.visit_order_id for candidate in result.candidates] == ["vo_department"]
                assert result.candidates[0].decision == "recommend"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_recording_match_reloads_recording_after_visit_order_sync_transaction_boundary(monkeypatch) -> None:
    async def fake_sync_visit_orders_for_context(db, **_kwargs):
        await db.rollback()

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        monkeypatch.setattr(
            "smart_badge_api.visit_order_matching.sync_visit_orders_for_context",
            fake_sync_visit_orders_for_context,
        )
        monkeypatch.setattr(
            "smart_badge_api.visit_order_matching.fetch_latest_remote_visit_order_date",
            lambda _hospital_codes: None,
        )

        try:
            async with session_factory() as db:
                staff = Staff(
                    id="staff_sync_boundary",
                    name="同步边界员工",
                    external_account="81000001",
                    hospital_code="6501",
                )
                recording = Recording(
                    id="rec_sync_boundary",
                    staff_id=staff.id,
                    file_name="20260506_153320.mp3",
                    file_path="/tmp/20260506_153320.mp3",
                    status="completed",
                    transcript_text="今天先看看皮肤状态，稍后再确认方案。",
                    created_at=datetime(2026, 5, 6, 15, 33, 20, tzinfo=UTC),
                )
                db.add_all([staff, recording])
                await db.commit()

            async with session_factory() as db:
                result = await analyze_recording_visit_order_match(
                    db,
                    "rec_sync_boundary",
                    apply_auto=False,
                    use_llm=False,
                )

                assert result is not None
                assert result.recording_id == "rec_sync_boundary"
                assert result.candidates == []
                assert result.summary == "2026-05-06 当天暂无可供推荐的到诊单候选。"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_role_specific_doctor_code_match_adds_evidence() -> None:
    staff = Staff(name="张医生", external_account="DOC001", is_doctor=True)
    order = VisitOrder(
        dzdh="DZ001",
        sjrq="2026-03-24",
        yyuer="DOC001",
        jzsj="14:00:00",
        ninam="李女士",
    )
    recording = Recording(
        file_name="doctor-audio.mp3",
        file_path="/tmp/doctor-audio.mp3",
        status="uploaded",
        transcript_text="医生先帮您面诊一下，看看适应症，再给您设计方案。",
    )
    recording.staff = staff

    candidate = _score_order_for_recording(
        recording,
        {"advisor_code": "ADV001", "start_seconds": 14 * 3600},
        order,
        recording.transcript_text,
        None,
        staff=staff,
    )

    assert any(item.label == "角色编码匹配" for item in candidate.evidence)
    assert candidate.confidence >= 0.14


def test_onsite_advisor_code_mismatch_applies_penalty() -> None:
    staff = Staff(name="钟露", external_account="86000995", is_onsite_advisor=True)
    recording = Recording(
        file_name="advisor-audio.mp3",
        file_path="/tmp/advisor-audio.mp3",
        status="uploaded",
        transcript_text="今天主要想咨询双眼皮修复，看看怎么设计方案。",
    )
    recording.staff = staff
    payload_meta = {"advisor_code": "86000995", "start_seconds": 15 * 3600 + 20 * 60}

    matched_order = VisitOrder(
        dzdh="DZ-MATCH",
        sjrq="2026-03-25",
        advxc="86000995",
        advxc_long="钟露",
        fzuer="86000995",
        fzr_id_dq="86000995",
        ninam="吴晓丽",
        customer_gender="女",
        fzsj="15:10:00",
        jzsj="15:30:00",
        remark_dz="双眼皮修复方案设计",
        jgks="外科",
    )
    other_order = VisitOrder(
        dzdh="DZ-MISMATCH",
        sjrq="2026-03-25",
        advxc="81047230",
        advxc_long="兰四秀",
        fzuer="81047230",
        fzr_id_dq="81047230",
        ninam="孙萍",
        customer_gender="女",
        fzsj="15:00:00",
        jzsj="15:30:00",
        remark_dz="双眼皮咨询，了解最低价位和设计方案",
        jgks="外科",
    )

    matched_candidate = _score_order_for_recording(
        recording,
        payload_meta,
        matched_order,
        recording.transcript_text,
        None,
        staff=staff,
    )
    other_candidate = _score_order_for_recording(
        recording,
        payload_meta,
        other_order,
        recording.transcript_text,
        None,
        staff=staff,
    )

    assert any(item.label == "角色编码匹配" for item in matched_candidate.evidence)
    assert any(item.label == "角色编码不一致" for item in other_candidate.evidence)
    assert matched_candidate.heuristic_score >= other_candidate.heuristic_score + 0.08


def test_heuristic_confidence_preserves_difference_above_previous_cap() -> None:
    higher = _OrderCandidate(visit_order=VisitOrder(dzdh="DZ-HIGH"), local_visit=None, heuristic_score=1.18)
    lower = _OrderCandidate(visit_order=VisitOrder(dzdh="DZ-LOW"), local_visit=None, heuristic_score=0.96)

    _finalize_candidate(higher)
    _finalize_candidate(lower)

    assert higher.confidence > lower.confidence
    assert higher.confidence > 0.89
    assert lower.confidence > 0.89
    assert _heuristic_confidence(1.18) == higher.confidence


def test_mutual_exclusion_prefers_candidate_with_identity_and_staff_alignment() -> None:
    top = _OrderCandidate(
        visit_order=VisitOrder(dzdh="DZ-TOP"),
        local_visit=None,
        heuristic_score=1.18,
        evidence=[
            MatchEvidenceOut(type="customer_name", label="客户称呼出现在录音", detail="录音中出现称呼「罗女士」", strength="high"),
            MatchEvidenceOut(type="role_code", label="角色编码匹配", detail="现场顾问编码一致", strength="high"),
            MatchEvidenceOut(type="time", label="分诊后理想时段", detail="录音创建时间在分诊时间20分钟之后", strength="high"),
        ],
    )
    second = _OrderCandidate(
        visit_order=VisitOrder(dzdh="DZ-SECOND"),
        local_visit=None,
        heuristic_score=1.15,
        evidence=[
            MatchEvidenceOut(type="role_code", label="角色编码匹配", detail="现场顾问编码一致", strength="high"),
            MatchEvidenceOut(type="time", label="分诊后理想时段", detail="录音创建时间在分诊时间14分钟之后", strength="high"),
            MatchEvidenceOut(type="project", label="咨询项目/关键词匹配", detail="匹配词：皮肤、治疗", strength="medium"),
        ],
    )
    third = _OrderCandidate(
        visit_order=VisitOrder(dzdh="DZ-THIRD"),
        local_visit=None,
        heuristic_score=1.08,
        evidence=[
            MatchEvidenceOut(type="role_code_mismatch", label="角色编码不一致", detail="顾问编码不一致", strength="medium"),
            MatchEvidenceOut(type="time", label="分诊时间非常接近", detail="录音创建时间在分诊时间8分钟之前", strength="high"),
            MatchEvidenceOut(type="project", label="咨询项目/关键词匹配", detail="匹配词：按照、效果", strength="high"),
        ],
    )

    for candidate in (top, second, third):
        _finalize_candidate(candidate)

    _apply_mutual_exclusion_orders([top, second, third])

    assert top.confidence > second.confidence
    assert second.confidence < 0.90
    assert third.confidence < second.confidence


def test_candidate_order_to_out_includes_merged_line_items_for_same_dzdh() -> None:
    order_primary = VisitOrder(
        id="vo_primary",
        dzdh="DZ-MERGED",
        dzseg="110",
        fzdh="DZ-MERGED-110",
        advxc="81020169",
        advxc_long="谢静",
        fzsj="08:45:15",
        jcsta_txt="已成交",
        remark_dz="双眼皮咨询",
    )
    order_companion = VisitOrder(
        id="vo_companion",
        dzdh="DZ-MERGED",
        dzseg="120",
        fzdh="DZ-MERGED-120",
        advxc="81021091",
        advxc_long="刘玲",
        fzsj="08:58:05",
        jcsta_txt="待跟进",
        remark_dz="二次分诊",
    )
    candidate = _OrderCandidate(
        visit_order=order_primary,
        local_visit=None,
        confidence=0.92,
        decision="recommend",
        reasons=["同一 DZDH 下存在多条分诊明细"],
        evidence=[MatchEvidenceOut(type="time", label="分诊时间接近", detail="分诊时间接近录音时间", strength="high")],
    )

    out = _candidate_order_to_out(
        candidate,
        {"DZ-MERGED": ["110", "120"]},
        None,
        {"DZ-MERGED": [order_primary, order_companion]},
    )

    assert out.dzdh == "DZ-MERGED"
    assert out.merged_segments == ["110", "120"]
    assert [item.dzseg for item in out.merged_line_items] == ["110", "120"]
    assert out.merged_line_items[0].triage_staff_name == "谢静"
    assert out.merged_line_items[1].note_summary == "到诊需求：二次分诊"


def test_customer_surname_plus_honorific_is_recognized() -> None:
    score, evidence, reason = _customer_name_match_signal("韩雪", "呃哪位是韩女士？请先到这边来。", "女")

    assert score >= 0.28
    assert evidence is not None
    assert evidence.label == "客户称呼出现在录音"
    assert reason is not None


def test_customer_full_name_match_has_higher_weight() -> None:
    score, evidence, reason = _customer_name_match_signal("韩雪", "韩雪您好，先和您确认一下今天的诉求。", "女")

    assert score >= 0.36
    assert evidence is not None
    assert evidence.label == "客户姓名出现在录音"
    assert reason is not None


def test_customer_surname_honorific_boosts_correct_candidate() -> None:
    staff = Staff(name="咨询师A", external_account="ADV010", is_onsite_advisor=True)
    recording = Recording(
        file_name="audio_140.mp3",
        file_path="/tmp/audio_140.mp3",
        status="uploaded",
        transcript_text="呃哪位是韩女士？我们先简单沟通一下您今天想了解什么。",
    )
    recording.staff = staff

    matched_order = VisitOrder(
        dzdh="DZ140-1",
        sjrq="2026-03-24",
        fzuer="ADV010",
        kunr="KH140-1",
        ninam="韩雪",
        customer_gender="女",
        fzsj="16:07:00",
        jzsj="16:17:00",
        remark_dz="热玛吉",
    )
    other_order = VisitOrder(
        dzdh="DZ140-2",
        sjrq="2026-03-24",
        fzuer="ADV010",
        kunr="KH140-2",
        ninam="李雪",
        customer_gender="女",
        fzsj="16:07:00",
        jzsj="16:17:00",
        remark_dz="热玛吉",
    )

    matched_candidate = _score_order_for_recording(
        recording,
        {"advisor_code": "ADV010", "start_seconds": 16 * 3600 + 10 * 60},
        matched_order,
        recording.transcript_text,
        None,
        staff=staff,
    )
    other_candidate = _score_order_for_recording(
        recording,
        {"advisor_code": "ADV010", "start_seconds": 16 * 3600 + 10 * 60},
        other_order,
        recording.transcript_text,
        None,
        staff=staff,
    )

    assert any(item.label == "客户称呼出现在录音" for item in matched_candidate.evidence)
    assert matched_candidate.heuristic_score > other_candidate.heuristic_score
    assert matched_candidate.heuristic_score >= 0.95


def test_addressed_identity_ignores_hospital_total_honorific_extension() -> None:
    transcript = "米兰柏羽总院这边会帮您安排，杜女士您先坐。"

    signals = _extract_addressed_identity_signals(transcript)
    conflicts = _build_identity_conflicts_for_candidate("杜佩洁", transcript)

    assert [item["token"] for item in signals] == ["杜女士"]
    assert conflicts == []


def test_addressed_identity_ignores_third_person_reference_to_xu_zong() -> None:
    transcript = "你好，这边坐嘛，是唐女士哈？这边坐嘛。她们都是问徐总，但是今天先给您看方案。"

    signals = _extract_addressed_identity_signals(transcript)
    conflicts = _build_identity_conflicts_for_candidate("唐仕凤", transcript)
    score, evidence, reason = _customer_name_match_signal("唐仕凤", transcript, "女")

    assert [item["token"] for item in signals] == ["唐女士"]
    assert conflicts == []
    assert score >= 0.28
    assert evidence is not None
    assert reason is not None


def test_project_conflict_penalizes_non_matching_order_theme() -> None:
    staff = Staff(name="咨询师A", external_account="ADV010", is_onsite_advisor=True)
    recording = Recording(
        file_name="audio_138.mp3",
        file_path="/tmp/audio_138.mp3",
        status="uploaded",
        transcript_text="今天主要想咨询瘦脸针和咬肌的问题，看看肉毒怎么打。",
    )
    recording.staff = staff
    conflict_order = VisitOrder(
        dzdh="DZ138-CONFLICT",
        sjrq="2026-03-24",
        fzuer="ADV010",
        ninam="周琴",
        remark_dz="陪妹妹周琴过来做眼袋手术，苹果肌复位+泪沟，注射方案：胶原+玻。",
        jgks="微整科",
    )

    candidate = _score_order_for_recording(
        recording,
        {"advisor_code": "ADV010", "start_seconds": 14 * 3600 + 24 * 60},
        conflict_order,
        recording.transcript_text,
        None,
        staff=staff,
    )

    assert any(item.label == "咨询项目明显不匹配" for item in candidate.evidence)
    assert candidate.heuristic_score < 0.2


def test_strong_identity_candidate_suppresses_other_candidates() -> None:
    staff = Staff(name="咨询师A", external_account="ADV010", is_onsite_advisor=True)
    recording = Recording(
        file_name="audio_138.mp3",
        file_path="/tmp/audio_138.mp3",
        status="uploaded",
        transcript_text="罗女士，您今天主要想咨询瘦脸针和咬肌的问题对吧？",
    )
    recording.staff = staff

    top_order = VisitOrder(
        dzdh="DZ138-1",
        sjrq="2026-03-24",
        fzuer="ADV010",
        ninam="罗雪",
        customer_gender="女",
        fzsj="14:18:00",
        jzsj="14:30:00",
        remark_dz="瘦脸针",
    )
    second_order = VisitOrder(
        dzdh="DZ138-2",
        sjrq="2026-03-24",
        fzuer="ADV010",
        ninam="王敏",
        customer_gender="女",
        fzsj="14:47:00",
        jzsj="15:10:00",
        remark_dz="热玛吉",
    )
    third_order = VisitOrder(
        dzdh="DZ138-3",
        sjrq="2026-03-24",
        fzuer="ADV010",
        ninam="周琴",
        customer_gender="女",
        fzsj="12:06:00",
        jzsj="17:43:00",
        remark_dz="眼袋手术，苹果肌复位+泪沟，胶原+玻眶周。",
    )

    top_candidate = _score_order_for_recording(
        recording,
        {"advisor_code": "ADV010", "start_seconds": 14 * 3600 + 24 * 60},
        top_order,
        recording.transcript_text,
        None,
        staff=staff,
    )
    second_candidate = _score_order_for_recording(
        recording,
        {"advisor_code": "ADV010", "start_seconds": 14 * 3600 + 24 * 60},
        second_order,
        recording.transcript_text,
        None,
        staff=staff,
    )
    third_candidate = _score_order_for_recording(
        recording,
        {"advisor_code": "ADV010", "start_seconds": 14 * 3600 + 24 * 60},
        third_order,
        recording.transcript_text,
        None,
        staff=staff,
    )

    candidates = [top_candidate, second_candidate, third_candidate]
    _apply_mutual_exclusion_orders(candidates)

    assert top_candidate.heuristic_score > second_candidate.heuristic_score
    assert top_candidate.heuristic_score > third_candidate.heuristic_score
    assert second_candidate.confidence < 0.35
    assert third_candidate.confidence < 0.35


def test_stage_extraction_uses_transcript_keywords() -> None:
    signals = _extract_recording_stage_signals("今天方案和报价都给您讲清楚了，可以先交定金。")

    assert "pricing" in signals


def test_order_stage_extraction_detects_pricing_and_followup() -> None:
    order = VisitOrder(
        dzdh="DZ002",
        dztyp_txt="复诊",
        jcsta_txt="已成交",
        remark_dz="术后恢复回访，今天补充报价说明。",
    )

    signals = _extract_order_stage_signals(order)

    assert "pricing" in signals
    assert "followup" in signals


def test_stage_alignment_is_wired_into_reverse_scoring() -> None:
    staff = Staff(name="顾问A", external_account="ADV010", is_onsite_advisor=True)
    order = VisitOrder(
        dzdh="DZ003",
        sjrq="2026-03-24",
        fzuer="ADV010",
        fzsj="13:00:00",
        jzsj="13:30:00",
        jcsta_txt="已成交",
    )
    recording = Recording(
        file_name="advisor-audio.mp3",
        file_path="/tmp/advisor-audio.mp3",
        status="uploaded",
        transcript_text="这个方案和价格我都给您讲完了，今天可以先交定金锁优惠。",
    )
    recording.staff = staff
    candidate = _score_recording_for_order(
        recording,
        {"advisor_code": "ADV010", "start_seconds": 13 * 3600 + 20 * 60},
        order,
        recording.transcript_text,
        None,
        staff=staff,
    )

    assert any(item.label == "接待阶段匹配" for item in candidate.evidence)
    assert candidate.confidence >= 0.2


def test_advisor_time_proximity_prefers_triage_time() -> None:
    staff = Staff(name="顾问A", external_account="ADV010", is_onsite_advisor=True)
    order = VisitOrder(
        dzdh="DZ141",
        fzsj="18:10:00",
        jzsj="19:46:00",
    )

    score, evidence = _time_proximity_score(18 * 3600 + 15 * 60, order, staff)

    assert score > 0
    assert evidence is not None
    assert evidence.label == "分诊后理想时段"
    assert "vs 分诊 18:10" in evidence.detail


def test_doctor_time_proximity_prefers_consult_time() -> None:
    staff = Staff(name="张医生", external_account="DOC001", is_doctor=True)
    order = VisitOrder(
        dzdh="DZ142",
        fzsj="18:10:00",
        jzsj="19:46:00",
    )

    score, evidence = _time_proximity_score(19 * 3600 + 40 * 60, order, staff)

    assert score > 0
    assert evidence is not None
    assert evidence.label == "接诊时间非常接近"
    assert "vs 接诊 19:46" in evidence.detail


def test_recording_order_scoring_boosts_start_time_weight() -> None:
    recording = Recording(
        file_name="time-weight.mp3",
        file_path="/tmp/time-weight.mp3",
        status="uploaded",
    )
    order = VisitOrder(
        dzdh="DZ-TIME",
        fzsj="14:10:00",
        jzsj="15:00:00",
    )

    candidate = _score_order_for_recording(
        recording,
        {"start_seconds": 14 * 3600 + 15 * 60},
        order,
        None,
        None,
        staff=None,
    )

    assert any(item.type == "time" for item in candidate.evidence)
    assert candidate.heuristic_score >= 0.40


def test_advisor_scoring_prefers_triage_time_more_strongly() -> None:
    staff = Staff(name="顾问A", external_account="ADV010", is_onsite_advisor=True)
    recording = Recording(
        file_name="advisor-time.mp3",
        file_path="/tmp/advisor-time.mp3",
        status="uploaded",
    )
    order = VisitOrder(
        dzdh="DZ-ADVISOR-TIME",
        fzsj="18:10:00",
        jzsj="19:46:00",
    )

    candidate = _score_order_for_recording(
        recording,
        {"start_seconds": 18 * 3600 + 15 * 60},
        order,
        None,
        None,
        staff=staff,
    )

    assert any(item.label == "分诊后理想时段" for item in candidate.evidence)
    assert candidate.heuristic_score >= 0.49


def test_doctor_scoring_prefers_consult_time_more_strongly() -> None:
    staff = Staff(name="张医生", external_account="DOC001", is_doctor=True)
    recording = Recording(
        file_name="doctor-time.mp3",
        file_path="/tmp/doctor-time.mp3",
        status="uploaded",
    )
    order = VisitOrder(
        dzdh="DZ-DOCTOR-TIME",
        fzsj="18:10:00",
        jzsj="19:46:00",
    )

    candidate = _score_order_for_recording(
        recording,
        {"start_seconds": 19 * 3600 + 50 * 60},
        order,
        None,
        None,
        staff=staff,
    )

    assert any(item.label == "接诊后理想时段" for item in candidate.evidence)
    assert candidate.heuristic_score >= 0.49


def test_role_time_window_penalizes_consultant_candidates_far_outside_window() -> None:
    staff = Staff(name="顾问A", external_account="ADV010", is_onsite_advisor=True)
    order = VisitOrder(
        dzdh="DZ_BAD_WINDOW",
        fzsj="15:00:00",
        jzsj="16:00:00",
        fzuer="ADV010",
    )

    score, evidence = _role_time_window_score(18 * 3600 + 15 * 60, 18 * 3600 + 44 * 60, order, staff)

    assert score < 0
    assert evidence is not None
    assert evidence.label == "录音时段偏离顾问接待窗口"


def test_consultant_wrong_window_reduces_candidate_confidence() -> None:
    staff = Staff(name="顾问A", external_account="ADV010", is_onsite_advisor=True)
    order = VisitOrder(
        dzdh="DZ005",
        sjrq="2026-03-24",
        fzuer="ADV010",
        kunr="KH001",
        ninam="客户甲",
        fzsj="15:00:00",
        jzsj="16:00:00",
        remark_dz="热玛吉",
    )
    recording = Recording(
        file_name="audio_143.mp3",
        file_path="/tmp/audio_143.mp3",
        status="uploaded",
        transcript_text="客户想了解热玛吉的方案。",
    )
    recording.staff = staff
    payload = {
        "consultAnalyzeResult": json.dumps(
            {
                "summary": {
                    "项目需求": {"content": "热玛吉"},
                }
            },
            ensure_ascii=False,
        )
    }

    candidate = _score_order_for_recording(
        recording,
        {"advisor_code": "ADV010", "customer_code": "KH001", "start_seconds": 18 * 3600 + 15 * 60},
        order,
        recording.transcript_text,
        payload,
        staff=staff,
    )

    assert any(item.label == "录音时段偏离顾问接待窗口" for item in candidate.evidence)
    assert candidate.hard_excluded is True
    assert candidate.decision == "ignore"
    assert candidate.confidence <= 0.19


def test_consultant_32_minute_window_mismatch_is_hard_excluded() -> None:
    staff = Staff(name="顾问A", external_account="ADV010", is_onsite_advisor=True)
    order = VisitOrder(
        dzdh="DZ005-32M",
        sjrq="2026-03-24",
        fzuer="ADV010",
        kunr="KH001",
        ninam="客户甲",
        fzsj="19:16:00",
        jzsj="20:00:00",
        remark_dz="热玛吉",
    )
    recording = Recording(
        file_name="audio_143.mp3",
        file_path="/tmp/audio_143.mp3",
        status="uploaded",
        transcript_text="客户想了解热玛吉的方案。",
    )
    recording.staff = staff

    candidate = _score_order_for_recording(
        recording,
        {"advisor_code": "ADV010", "customer_code": "KH001", "start_seconds": 18 * 3600 + 15 * 60},
        order,
        recording.transcript_text,
        None,
        staff=staff,
    )

    assert any(item.label == "录音时段偏离顾问接待窗口" for item in candidate.evidence)
    assert candidate.hard_excluded is True
    assert candidate.decision == "ignore"
    assert candidate.confidence <= 0.19


def test_hard_excluded_candidate_is_not_re_elevated_by_llm() -> None:
    staff = Staff(name="顾问A", external_account="ADV010", is_onsite_advisor=True)
    order = VisitOrder(
        id="vo-hard-1",
        dzdh="DZ006",
        sjrq="2026-03-24",
        fzuer="ADV010",
        kunr="KH002",
        fzsj="15:00:00",
        jzsj="16:00:00",
        remark_dz="热玛吉",
    )
    recording = Recording(
        file_name="audio_143.mp3",
        file_path="/tmp/audio_143.mp3",
        status="uploaded",
        transcript_text="客户想了解热玛吉的方案。",
    )
    recording.staff = staff
    candidate = _score_order_for_recording(
        recording,
        {"advisor_code": "ADV010", "customer_code": "KH002", "start_seconds": 18 * 3600 + 15 * 60},
        order,
        recording.transcript_text,
        None,
        staff=staff,
    )

    _merge_llm_order_result(
        [candidate],
        {
            "ranked_candidates": [
                {
                    "candidate_id": order.id,
                    "confidence": 0.97,
                    "reasons": ["LLM 误抬高"],
                    "evidence": [{"label": "LLM", "detail": "should be ignored", "strength": "high"}],
                }
            ]
        },
    )

    assert candidate.hard_excluded is True
    assert candidate.decision == "ignore"
    assert candidate.confidence <= 0.19
    assert candidate.excluded_reasons


def test_llm_excluded_reasons_are_exposed_for_ignored_candidates() -> None:
    winner = _OrderCandidate(
        visit_order=VisitOrder(id="vo-best", dzdh="DZ-BEST"),
        local_visit=None,
        confidence=0.82,
        decision="recommend",
    )
    loser = _OrderCandidate(
        visit_order=VisitOrder(id="vo-other", dzdh="DZ-OTHER"),
        local_visit=None,
        confidence=0.31,
        decision="ignore",
    )

    _merge_llm_order_result(
        [winner, loser],
        {
            "ranked_candidates": [
                {
                    "candidate_id": "vo-best",
                    "confidence": 0.91,
                    "reasons": ["客户称呼和时间窗口都更吻合"],
                    "evidence": [{"label": "最佳候选", "detail": "身份与内容双重命中", "strength": "high"}],
                },
                {
                    "candidate_id": "vo-other",
                    "confidence": 0.18,
                    "reasons": ["不是最佳匹配"],
                    "excluded_reasons": ["客户称呼没有命中", "项目主题也弱于更优候选"],
                    "evidence": [{"label": "弱候选", "detail": "只有日期接近", "strength": "low"}],
                },
            ]
        },
    )

    assert winner.excluded_reasons == []
    assert loser.decision == "ignore"
    assert loser.excluded_reasons == ["客户称呼没有命中", "项目主题也弱于更优候选"]


def test_llm_merge_preserves_strong_identity_reason_and_address_analysis() -> None:
    winner = _OrderCandidate(
        visit_order=VisitOrder(id="vo-du", dzdh="DZ-DU", ninam="杜佩洁"),
        local_visit=None,
        confidence=0.82,
        decision="recommend",
        reasons=["录音时间与分诊时间接近（差6分钟）"],
        evidence=[
            MatchEvidenceOut(
                type="customer_name",
                label="客户称呼出现在录音",
                detail="录音中出现称呼「杜女士」",
                strength="high",
            )
        ],
    )

    _merge_llm_order_result(
        [winner],
        {
            "ranked_candidates": [
                {
                    "candidate_id": "vo-du",
                    "confidence": 0.91,
                    "reasons": ["时间窗口和项目需求都吻合"],
                    "customer_address_analysis": {
                        "matched_signals": ["杜女士"],
                        "conflicting_signals": ["郭女士"],
                        "conclusion": "LLM 判断后续的“杜女士”更可信，“郭女士”更像开场口误。",
                    },
                    "evidence": [{"label": "最佳候选", "detail": "身份与内容双重命中", "strength": "high"}],
                }
            ]
        },
    )

    assert any("杜女士" in reason for reason in winner.reasons)
    assert any(item.label == "LLM客户称呼分析" for item in winner.evidence)


def test_extract_companion_customer_codes_from_visit_order_remarks() -> None:
    order = VisitOrder(
        dzdh="DZ136-1",
        kunr="60895193",
        remark_dz="同行72175385",
    )

    assert _extract_companion_customer_codes(order) == ["72175385"]


def test_find_companion_orders_uses_mutual_customer_code_references() -> None:
    older_sister = VisitOrder(dzdh="DZ136-1", kunr="72175385", remark_dz="同行60895193")
    younger_sister = VisitOrder(dzdh="DZ136-2", kunr="60895193", remark_dz="同行72175385")
    unrelated = VisitOrder(dzdh="DZ136-3", kunr="00000001", remark_dz="单独到院")

    companion_orders = _find_companion_orders(younger_sister, [older_sister, younger_sister, unrelated])

    assert [item.dzdh for item in companion_orders] == ["DZ136-1"]


def test_companion_visit_signal_rewards_mutual_references_even_before_binding() -> None:
    order = VisitOrder(dzdh="DZ136-2", kunr="60895193", remark_dz="同行72175385")
    companion = VisitOrder(dzdh="DZ136-1", kunr="72175385", remark_dz="同行60895193")

    score, evidence, reason = _companion_visit_signal("你们姐妹两个今天一起过来。", order, [companion])

    assert score > 0
    assert evidence is not None
    assert evidence.label == "同行到诊单互指"
    assert reason is not None


def test_order_shortlist_prefers_advisor_code_and_time_window() -> None:
    staff = Staff(name="顾问A", external_account="ADV010", is_onsite_advisor=True)
    recording = Recording(file_name="shortlist-audio-1", file_path="/tmp/shortlist-audio-1.mp3", status="uploaded")
    recording.staff = staff
    orders = [
        VisitOrder(dzdh="DZ-LATE", sjrq="2026-03-24", fzuer="ADV010", fzsj="18:00:00", jzsj="19:00:00"),
        VisitOrder(dzdh="DZ-BEST", sjrq="2026-03-24", fzuer="ADV010", fzsj="14:20:00", jzsj="14:40:00"),
        VisitOrder(dzdh="DZ-OTHER", sjrq="2026-03-24", fzuer="ADV999", fzsj="14:22:00", jzsj="14:45:00"),
    ]

    shortlisted = _shortlist_orders_for_recording(
        recording,
        orders,
        {"advisor_code": "ADV010", "start_seconds": 14 * 3600 + 24 * 60},
        staff,
    )

    assert shortlisted[0][0].dzdh == "DZ-BEST"
    assert all(item[0].dzdh != "DZ-OTHER" for item in shortlisted[:2])


def test_order_shortlist_allows_nearer_time_candidate_to_outrank_far_role_match() -> None:
    staff = Staff(name="顾问A", external_account="ADV010", is_onsite_advisor=True)
    recording = Recording(file_name="shortlist-audio-2", file_path="/tmp/shortlist-audio-2.mp3", status="uploaded")
    recording.staff = staff
    orders = [
        VisitOrder(dzdh="DZ-FAR-ROLE", sjrq="2026-03-24", fzuer="ADV010", fzsj="18:00:00", jzsj="18:30:00"),
        VisitOrder(dzdh="DZ-NEAR-TIME", sjrq="2026-03-24", fzuer="ADV999", fzsj="14:20:00", jzsj="14:45:00"),
    ]

    shortlisted = _shortlist_orders_for_recording(
        recording,
        orders,
        {"advisor_code": "ADV010", "start_seconds": 14 * 3600 + 24 * 60},
        staff,
    )

    assert shortlisted[0][0].dzdh == "DZ-NEAR-TIME"


def test_order_shortlist_facts_mark_direct_customer_and_role_matches() -> None:
    staff = Staff(name="张医生", external_account="DOC001", is_doctor=True)
    order = VisitOrder(
        dzdh="DZ-DOCTOR",
        kunr="KH001",
        yyuer="DOC001",
        jzsj="15:00:00",
    )

    facts = _build_order_shortlist_facts(
        order,
        {"visit_order_no": "DZ-DOCTOR", "customer_code": "KH001", "start_seconds": 15 * 3600 + 10 * 60},
        staff,
        Recording(file_name="doctor-shortlist", file_path="/tmp/doctor-shortlist.mp3", status="uploaded"),
    )

    assert facts["direct_match"] is True
    assert facts["customer_match"] is True
    assert facts["role_match"] is True
    assert facts["time_diff_minutes"] == 10


def test_structured_demands_extract_project_and_need_terms() -> None:
    payload = {
        "consultAnalyzeResult": json.dumps(
            {
                "summary": {
                    "客户档案": {
                        "需求关键词": {"content": ["抗衰", "法令纹"]},
                        "核心需求": {"content": "希望改善松弛"},
                    },
                    "项目需求": {"content": "热玛吉"},
                    "核心诉求": {"content": "恢复期短"},
                }
            },
            ensure_ascii=False,
        ),
        "requirementAnalyzeResult": json.dumps(
            {
                "summary": {
                    "面诊与设计方案": {
                        "推荐方案清单": {"content": "热玛吉联合超声提升"},
                    },
                    "需求与动机分析": {
                        "核心诉求": {"content": "自然提升，不想开刀"},
                    },
                }
            },
            ensure_ascii=False,
        ),
    }

    demands = _extract_structured_demands(payload)

    assert "热玛吉" in demands["project_terms"]
    assert "恢复期短" in demands["need_terms"]
    assert "面诊与设计方案" in demands["areas"]


def test_structured_demand_alignment_is_wired_into_order_scoring() -> None:
    order = VisitOrder(
        dzdh="DZ004",
        sjrq="2026-03-24",
        remark_dz="热玛吉抗衰提升，客户希望恢复期短，自然一点，不想做开刀项目，重点关注法令纹和松弛",
    )
    recording = Recording(
        file_name="demand-audio.mp3",
        file_path="/tmp/demand-audio.mp3",
        status="uploaded",
        transcript_text="她主要想做抗衰，希望恢复期短，最好自然一点。",
    )
    payload = {
        "consultAnalyzeResult": json.dumps(
            {
                "summary": {
                    "客户档案": {
                        "需求关键词": {"content": ["抗衰", "法令纹"]},
                        "核心需求": {"content": "希望改善松弛"},
                    },
                    "项目需求": {"content": "热玛吉"},
                    "核心诉求": {"content": "恢复期短，自然提升"},
                }
            },
            ensure_ascii=False,
        )
    }

    candidate = _score_order_for_recording(
        recording,
        {"advisor_code": "ADV011", "start_seconds": 14 * 3600},
        order,
        recording.transcript_text,
        payload,
        staff=None,
    )

    assert any(item.label == "结构化诉求匹配" for item in candidate.evidence)
    assert candidate.confidence >= 0.08


def test_extract_payload_demographics_can_infer_male_from_transcript() -> None:
    demographics = _extract_payload_demographics(
        None,
        "我之前下巴打过玻尿酸，但男生的话太尖会显得不自然，很多帅哥都会介意，男性脸型更需要自然一点。",
    )

    assert demographics["gender"] == "男"
    assert demographics["gender_source"] == "transcript"
    assert demographics["gender_strength"] == "strong"


def test_recording_created_at_and_male_gender_boost_correct_order() -> None:
    staff = Staff(name="兰四秀", external_account="81047230", is_onsite_advisor=True)
    recording = Recording(
        file_name="audio_119",
        file_path="/tmp/audio_119.mp3",
        status="uploaded",
        created_at=datetime(2026, 3, 17, 7, 19, 33, tzinfo=UTC),
        transcript_text="我之前下巴打过波波，但男生就有点不好看，很多帅哥都会介意，所以这次想再调自然一点。",
    )
    recording.staff = staff

    matched_order = VisitOrder(
        dzdh="2118038927",
        sjrq="2026-03-17",
        fzuer="81047230",
        advxc="81047230",
        ninam="王子为",
        customer_gender="男",
        fzsj="15:05:21",
        jzsj="16:50:30",
        remark_dz="注射微调",
    )
    other_order = VisitOrder(
        dzdh="2118039711",
        sjrq="2026-03-17",
        fzuer="81047230",
        advxc="81047230",
        ninam="田时先",
        customer_gender="女",
        fzsj="15:36:00",
        jzsj="16:20:00",
        remark_dz="注射微调",
    )

    matched_candidate = _score_order_for_recording(
        recording,
        None,
        matched_order,
        recording.transcript_text,
        None,
        staff=staff,
    )
    other_candidate = _score_order_for_recording(
        recording,
        None,
        other_order,
        recording.transcript_text,
        None,
        staff=staff,
    )

    labels = {item.label for item in matched_candidate.evidence}

    assert matched_candidate.heuristic_score > other_candidate.heuristic_score
    assert "分诊后理想时段" in labels
    assert "录音时段落入顾问接待窗口" in labels
    assert "男性客户特征吻合" in labels
    assert any(item.label == "客户性别冲突(档案)" for item in other_candidate.evidence)
    assert matched_candidate.heuristic_score - other_candidate.heuristic_score >= 0.90


def test_strong_male_vs_female_gender_conflict_has_heavier_penalty() -> None:
    staff = Staff(name="钟露", external_account="86000995", is_onsite_advisor=True)
    recording = Recording(
        file_name="audio_gender_conflict",
        file_path="/tmp/audio_gender_conflict.mp3",
        status="uploaded",
        created_at=datetime(2026, 3, 15, 4, 12, 30, tzinfo=UTC),
        transcript_text="男士这边先坐，我们主要还是先看鼻子的方案，男性做直鼻会更自然一点。",
    )
    recording.staff = staff

    conflict_order = VisitOrder(
        dzdh="2118025971",
        dzseg="110",
        sjrq="2026-03-15",
        advxc="86000995",
        fzuer="86000995",
        fzr_id_dq="86000995",
        ninam="王晓晓",
        customer_gender="女",
        fzsj="12:01:00",
        jzsj="12:20:00",
        jgks="外科",
        remark_dz="咨询外科鼻整形，直鼻微翘方案",
    )
    matched_order = VisitOrder(
        dzdh="2118025972",
        dzseg="110",
        sjrq="2026-03-15",
        advxc="86000995",
        fzuer="86000995",
        fzr_id_dq="86000995",
        ninam="王小伟",
        customer_gender="男",
        fzsj="12:01:00",
        jzsj="12:20:00",
        jgks="外科",
        remark_dz="咨询外科鼻整形，直鼻微翘方案",
    )

    conflict_candidate = _score_order_for_recording(
        recording,
        {"advisor_code": "86000995", "start_seconds": 12 * 3600 + 12 * 60},
        conflict_order,
        recording.transcript_text,
        None,
        staff=staff,
    )
    matched_candidate = _score_order_for_recording(
        recording,
        {"advisor_code": "86000995", "start_seconds": 12 * 3600 + 12 * 60},
        matched_order,
        recording.transcript_text,
        None,
        staff=staff,
    )

    conflict_evidence = next(item for item in conflict_candidate.evidence if item.label == "客户性别冲突(档案)")

    assert "档案性别=女, 录音推断=男" in conflict_evidence.detail
    assert matched_candidate.heuristic_score - conflict_candidate.heuristic_score >= 0.45


def test_opening_misaddress_does_not_force_female_gender_inference() -> None:
    demographics = _extract_payload_demographics(
        None,
        "看点是张雪荣女士，唉对。那个妹妹已经面诊了，噢已经面过了是吧？现在来看方案的是另外一个客户。",
    )

    assert demographics["gender"] is None


def test_eye_surgery_plan_match_outranks_followup_order() -> None:
    staff = Staff(name="钟露", external_account="86000995", is_onsite_advisor=True)
    recording = Recording(
        file_name="audio_117",
        file_path="/tmp/audio_117.mp3",
        status="uploaded",
        created_at=datetime(2026, 3, 17, 6, 35, 9, tzinfo=UTC),
        transcript_text=(
            "看点是张雪荣女士，唉对。那个妹妹已经面诊了，噢已经面过了是吧？"
            "现在鲁院的方案就是一个双眼皮、内眼角加一个轻度的提肌调整，"
            "他们觉得有点超预算，我想再沟通一下价格和什么时候做。"
        ),
    )
    recording.staff = staff

    matched_order = VisitOrder(
        dzdh="2118038484",
        sjrq="2026-03-17",
        fzuer="86000995",
        advxc="86000995",
        ninam="玲",
        customer_gender="女",
        fzsj="14:20:00",
        jzsj="21:33:00",
        remark_dz="重睑+开内眦+去皮去脂调肌力，意向卢，费用超预算，铺垫齐，预算one",
    )
    followup_order = VisitOrder(
        dzdh="2118038529",
        sjrq="2026-03-17",
        fzuer="86000995",
        advxc="86000995",
        ninam="胡林",
        customer_gender="女",
        fzsj="14:25:00",
        jzsj="16:29:00",
        remark_dz="拆线",
        jgks="外科",
    )

    matched_candidate = _score_order_for_recording(
        recording,
        None,
        matched_order,
        recording.transcript_text,
        None,
        staff=staff,
    )
    followup_candidate = _score_order_for_recording(
        recording,
        None,
        followup_order,
        recording.transcript_text,
        None,
        staff=staff,
    )

    assert matched_candidate.heuristic_score > followup_candidate.heuristic_score
    assert any(item.label == "术式方案匹配" for item in matched_candidate.evidence)
    assert any(item.label == "接待阶段明显不匹配" for item in followup_candidate.evidence)
