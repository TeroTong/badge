import json

from smart_badge_api.analysis.agent_pipeline import _CORRECTION_AGENT_SYSTEM_PROMPT, _line_needs_correction_context
from smart_badge_api.analysis.staged_pipeline import (
    _CORRECTION_SYSTEM_PROMPT,
    _apply_correction_patch,
    _build_line_speaker_metadata,
)
from smart_badge_api.analysis.transcript import extract_transcript_segments


def test_agent_correction_prompt_weakens_badge_owner_prior() -> None:
    assert "工牌本人/咨询师" in _CORRECTION_AGENT_SYSTEM_PROMPT
    assert "离工牌更近的医生误标成“工牌本人”" in _CORRECTION_AGENT_SYSTEM_PROMPT
    assert "真实工牌佩戴者/咨询师和客户混到同一个客户侧 speaker" in _CORRECTION_AGENT_SYSTEM_PROMPT
    assert "优先把前者判为 doctor" in _CORRECTION_AGENT_SYSTEM_PROMPT


def test_agent_correction_prompt_requires_line_level_mixed_speaker_repair() -> None:
    assert "不要把整个 speaker_role_map 统一成客户" in _CORRECTION_AGENT_SYSTEM_PROMPT
    assert "speaker_corrections 逐句修正" in _CORRECTION_AGENT_SYSTEM_PROMPT
    assert "我们都记了" in _CORRECTION_AGENT_SYSTEM_PROMPT
    assert "还有什么想问我的吗" in _CORRECTION_AGENT_SYSTEM_PROMPT


def test_staged_correction_prompt_keeps_same_role_boundaries() -> None:
    assert "Badge recordings may mark a doctor near the badge as the" in _CORRECTION_SYSTEM_PROMPT
    assert "do not force one" in _CORRECTION_SYSTEM_PROMPT
    assert "prefer doctor for the first speaker" in _CORRECTION_SYSTEM_PROMPT
    assert "我们都记了" in _CORRECTION_SYSTEM_PROMPT


def test_archive_utterance_extraction_preserves_raw_speaker_ids() -> None:
    raw = {
        "utterances": [
            {
                "speaker": "consultant",
                "speaker_id": "SPEAKER_00",
                "speaker_display_label": "李宇晴（工牌本人）",
                "speaker_role": "consultant",
                "speaker_business_role": "badge_owner",
                "speaker_role_source": "local_heuristic",
                "asr_original_speaker": "speaker_0",
                "asr_original_speaker_id": "speaker_0",
                "begin_ms": 0,
                "end_ms": 1000,
                "text": "这个地方的深层填充先把骨头表面打上一些填充剂。",
            }
        ]
    }
    segment = extract_transcript_segments(json.loads(json.dumps(raw, ensure_ascii=False)))[0]
    assert segment["speaker_id"] == "SPEAKER_00"
    assert segment["asr_original_speaker_id"] == "speaker_0"
    assert segment["speaker_role_source"] == "local_heuristic"


def test_line_speaker_metadata_prefers_diarized_speaker_id_over_asr_original() -> None:
    raw = {
        "utterances": [
            {
                "speaker": "consultant",
                "speaker_id": "SPEAKER_02",
                "speaker_display_label": "同行人",
                "speaker_role": "customer",
                "speaker_business_role": "primary_customer",
                "asr_original_speaker_id": "speaker_0",
                "begin_ms": 0,
                "end_ms": 1000,
                "text": "一点都没影响。",
            }
        ]
    }
    metadata = _build_line_speaker_metadata("[00:00-00:01] 客户（同行人）: 一点都没影响。", raw)
    assert metadata["L0001"]["asr_speaker"] == "SPEAKER_02"


def test_customer_labeled_staff_cues_are_kept_in_focused_correction_windows() -> None:
    line = (
        "L0032 [asr_speaker=speaker_2; current_role=customer]: "
        "[02:40-02:42] 客户（同行人）: 那姐姐刚刚做的热玛吉不影响啊。"
    )
    assert _line_needs_correction_context(line, {"role": "customer"}) is True


def test_apply_correction_patch_repairs_doctor_like_badge_owner_when_customer_speaker_is_mixed() -> None:
    line_map = {
        "L0001": "[00:00-00:08] 咨询师（李宇晴（工牌本人））: 通过注射实现，深层填充到骨头表面。",
        "L0002": "[00:09-00:19] 咨询师（李宇晴（工牌本人））: 先做骨性支撑，再做玻尿酸和童颜针。",
        "L0003": "[00:19-00:26] 咨询师（李宇晴（工牌本人））: 内侧苹果肌和鼻基底要分层注射材料。",
        "L0004": "[00:26-00:31] 咨询师（李宇晴（工牌本人））: 每边一支瑞德喜，下巴两支玻尿酸。",
        "L0005": "[00:31-00:35] 咨询师（李宇晴（工牌本人））: 不含玻尿酸，避免发黄和馒化。",
        "L0006": "[00:35-00:39] 客户（同行人）: 那姐姐刚刚做的热玛吉不影响啊。",
    }
    numbered = "\n".join(f"{line_id}: {line}" for line_id, line in line_map.items())
    metadata = {
        line_id: {
            "asr_speaker": "speaker_0" if line_id != "L0006" else "speaker_2",
            "speaker_label": "李宇晴（工牌本人）" if line_id != "L0006" else "同行人",
            "role": "consultant" if line_id != "L0006" else "customer",
        }
        for line_id in line_map
    }
    patch = {
        "speaker_role_map": [
            {
                "asr_speaker": "speaker_2",
                "role": "companion",
                "participant_label": "陪同人员",
                "customer_scope": "companion_or_family",
                "confidence": 0.8,
            }
        ]
    }
    corrected, correction_metadata = _apply_correction_patch(numbered, line_map, patch, metadata)

    assert "L0001: [00:00-00:08] 医生:" in corrected
    assert "L0006: [00:35-00:39] 咨询师:" in corrected
    assert len(correction_metadata["auto_speaker_role_repairs"]) == 6
    assert correction_metadata["skipped_speaker_role_maps"][0]["asr_speaker"] == "speaker_2"
    assert "陪同人员" not in corrected
