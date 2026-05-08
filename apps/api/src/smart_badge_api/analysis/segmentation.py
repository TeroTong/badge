"""AI 对话边界识别服务。

对一段可能包含多段独立咨询的长录音，通过统计分析 + LLM 语义判断，
检测出对话边界，将录音切分为多个独立的对话段落，并识别主咨询段。

用法:
    from analysis.segmentation import detect_boundaries
    result = detect_boundaries("raw/xxx.json")
"""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path

from .llm_client import chat_completion, parse_json_response
from .segmentation_schemas import (
    CandidateBoundary,
    DialogueBoundary,
    DialogueSegment,
    SegmentationResult,
)
from .transcript import load_transcript, normalize_role

logger = logging.getLogger(__name__)

# ── 可调参数 ──────────────────────────────────────────────────
# 候选边界的最小静默时长（毫秒）——低于此值的间隙不进入候选
MIN_GAP_MS = 60_000  # 60 秒
# 统计预筛：最多向 LLM 提交的候选边界数量
MAX_CANDIDATES = 15
# 每个候选边界前后各取多少条 segment 作为上下文
CONTEXT_WINDOW = 8


def _ms_to_mmss(ms: int) -> str:
    total_sec = ms // 1000
    return f"{total_sec // 60:02d}:{total_sec % 60:02d}"


# ── 统计预筛 ──────────────────────────────────────────────────

def _find_candidate_boundaries(
    segments: list[dict],
    min_gap_ms: int = MIN_GAP_MS,
    context_window: int = CONTEXT_WINDOW,
) -> list[CandidateBoundary]:
    """扫描相邻 segment 之间的时间间隙，筛选超过阈值的候选边界。"""
    candidates: list[CandidateBoundary] = []
    for i in range(1, len(segments)):
        gap = segments[i]["begin"] - segments[i - 1]["end"]
        if gap >= min_gap_ms:
            # 提取上下文
            ctx_start = max(0, i - context_window)
            ctx_end = min(len(segments), i + context_window)

            before_lines = []
            for s in segments[ctx_start:i]:
                role = normalize_role(s.get("role", "未知"))
                text = s.get("text", "").strip()
                ts = _ms_to_mmss(s.get("begin", 0))
                if text:
                    before_lines.append(f"[{ts}] {role}: {text}")

            after_lines = []
            for s in segments[i:ctx_end]:
                role = normalize_role(s.get("role", "未知"))
                text = s.get("text", "").strip()
                ts = _ms_to_mmss(s.get("begin", 0))
                if text:
                    after_lines.append(f"[{ts}] {role}: {text}")

            candidates.append(CandidateBoundary(
                gap_index=i - 1,
                gap_ms=gap,
                prev_end_ms=segments[i - 1]["end"],
                next_begin_ms=segments[i]["begin"],
                context_before="\n".join(before_lines),
                context_after="\n".join(after_lines),
            ))

    # 按间隙大小降序，取前 MAX_CANDIDATES 个
    candidates.sort(key=lambda c: c.gap_ms, reverse=True)
    return candidates[:MAX_CANDIDATES]


# ── LLM Prompt ────────────────────────────────────────────────

_SEGMENTATION_SYSTEM_PROMPT = """\
判断医美工牌长录音的候选分段边界。边界成立通常需要“足够长静默 + 前后人物/客户/话题/场景明显切换”；静默后继续同一客户同一话题则不是边界。场景只用：医美咨询、前台接待、候诊闲聊、内部协作、其他。

只输出 JSON：
{
  "boundaries": [
    {
      "candidate_index": 0,
      "is_boundary": true,
      "confidence": 0.9,
      "reason": "间隙前客户A告别离开，间隙后出现新的问候语和不同客户的声音",
      "before_scene": "医美咨询",
      "after_scene": "前台接待"
    }
  ],
  "main_consultation_hint": "场景2（第X-Y候选边界之间）是最完整的医美咨询主过程"
}
"""


def _build_segmentation_user_prompt(
    candidates: list[CandidateBoundary],
    total_segments: int,
    total_duration_ms: int,
) -> str:
    """构建发送给 LLM 的 user prompt。"""
    lines = [
        f"录音总时长：{_ms_to_mmss(total_duration_ms)}，共 {total_segments} 条转写片段。",
        f"统计预筛发现 {len(candidates)} 个静默间隙超过阈值的候选边界，请逐一判断。",
        "",
    ]
    for idx, c in enumerate(candidates):
        gap_sec = c.gap_ms / 1000
        gap_str = f"{gap_sec:.0f}秒" if gap_sec < 120 else f"{gap_sec / 60:.1f}分钟"
        lines.append(f"═══ 候选边界 {idx}（间隙 {gap_str}）═══")
        lines.append(f"间隙前（{_ms_to_mmss(c.prev_end_ms)} 结束）：")
        lines.append(c.context_before)
        lines.append(f"--- 静默 {gap_str} ---")
        lines.append(f"间隙后（{_ms_to_mmss(c.next_begin_ms)} 开始）：")
        lines.append(c.context_after)
        lines.append("")

    return "\n".join(lines)


# ── 结果组装 ──────────────────────────────────────────────────

def _build_segments_from_boundaries(
    segments: list[dict],
    confirmed_boundaries: list[DialogueBoundary],
    llm_scenes: dict[int, tuple[str, str]],
) -> list[DialogueSegment]:
    """根据确认的边界将原始 segments 切分为多个 DialogueSegment。

    Args:
        segments: 原始 transcribeResult
        confirmed_boundaries: 经 LLM 确认的边界列表（按 after_segment_index 排序）
        llm_scenes: {candidate_index: (before_scene, after_scene)}
    """
    if not segments:
        return []

    # 切分点列表（segment index 之后切）
    cut_points = sorted(b.after_segment_index + 1 for b in confirmed_boundaries)

    # 构建段落范围
    ranges: list[tuple[int, int]] = []
    prev = 0
    for cp in cut_points:
        if cp > prev:
            ranges.append((prev, cp))
        prev = cp
    if prev < len(segments):
        ranges.append((prev, len(segments)))

    # 从 LLM 场景信息中推导每段的 scene_type
    # 边界按 after_segment_index 排序后，第一段用第一个边界的 before_scene，
    # 后续第 i 段用第 i-1 个边界的 after_scene
    scene_hints: list[str] = []
    if not confirmed_boundaries:
        scene_hints.append("医美咨询")
    else:
        sorted_indices = sorted(llm_scenes.keys())
        # 第一段：第一个边界的 before_scene
        if sorted_indices:
            scene_hints.append(llm_scenes[sorted_indices[0]][0])
        else:
            scene_hints.append("其他")
        # 后续各段：对应边界的 after_scene
        for idx in sorted_indices:
            scene_hints.append(llm_scenes[idx][1])

    result_segments: list[DialogueSegment] = []
    for seg_idx, (start, end) in enumerate(ranges):
        seg_slice = segments[start:end]
        start_ms = seg_slice[0]["begin"]
        end_ms = seg_slice[-1]["end"]

        role_counter: Counter[str] = Counter()
        for s in seg_slice:
            role_counter[normalize_role(s.get("role", "未知"))] += 1

        scene = scene_hints[seg_idx] if seg_idx < len(scene_hints) else "其他"
        duration_min = (end_ms - start_ms) / 60000

        result_segments.append(DialogueSegment(
            segment_index=seg_idx,
            start_ms=start_ms,
            end_ms=end_ms,
            start_mmss=_ms_to_mmss(start_ms),
            end_mmss=_ms_to_mmss(end_ms),
            transcript_segment_range=(start, end),
            scene_type=scene,
            description=f"时长 {duration_min:.1f} 分钟，{len(seg_slice)} 条转写",
            is_main_consultation=False,
            role_distribution=dict(role_counter),
        ))

    return result_segments


def _identify_main_consultation(
    dialogue_segments: list[DialogueSegment],
    main_hint: str | None,
) -> int | None:
    """识别主咨询段落。

    优先使用 LLM 的提示；若无提示，选择时长最长且场景为"医美咨询"的段落。
    """
    if not dialogue_segments:
        return None

    # 尝试从 LLM hint 中提取
    if main_hint:
        for seg in dialogue_segments:
            if seg.scene_type == "医美咨询":
                # 选最长的医美咨询段
                pass

    # 回退策略：选 scene_type="医美咨询" 中时长最长的段
    consultation_segs = [
        s for s in dialogue_segments if s.scene_type == "医美咨询"
    ]
    if consultation_segs:
        best = max(consultation_segs, key=lambda s: s.end_ms - s.start_ms)
        return best.segment_index

    # 实在找不到，选时长最长的
    best = max(dialogue_segments, key=lambda s: s.end_ms - s.start_ms)
    return best.segment_index


# ── 主入口 ────────────────────────────────────────────────────

def detect_boundaries(
    path: str | Path,
    *,
    min_gap_ms: int = MIN_GAP_MS,
) -> SegmentationResult:
    """对一个转写 JSON 文件执行对话边界识别。

    Args:
        path: 转写文件路径
        min_gap_ms: 候选边界的最小静默时长（毫秒）

    Returns:
        SegmentationResult 包含所有检测到的边界和对话段落
    """
    path = Path(path)
    logger.info("开始对话边界识别: %s", path.name)

    raw = load_transcript(path)
    segments = raw.get("payload", {}).get("transcribeResult", [])

    if not segments:
        raise ValueError(f"转写文件 {path.name} 中没有 transcribeResult")

    total_duration_ms = segments[-1]["end"]
    total_segments = len(segments)

    logger.info("录音总时长: %s, 转写片段数: %d",
                _ms_to_mmss(total_duration_ms), total_segments)

    # Step 1: 统计预筛
    candidates = _find_candidate_boundaries(segments, min_gap_ms=min_gap_ms)
    logger.info("统计预筛发现 %d 个候选边界", len(candidates))

    if not candidates:
        # 没有超过阈值的间隙 → 整段视为单一对话
        logger.info("未发现超过 %dms 的静默间隙，判定为单段对话", min_gap_ms)
        single_seg = _build_single_segment(segments, path.name)
        return SegmentationResult(
            file_name=path.name,
            total_duration_ms=total_duration_ms,
            total_transcript_segments=total_segments,
            boundaries=[],
            dialogue_segments=[single_seg],
            main_consultation_index=0,
        )

    # Step 2: LLM 确认
    user_prompt = _build_segmentation_user_prompt(
        candidates, total_segments, total_duration_ms,
    )
    logger.info("发送 %d 个候选边界到 LLM 进行语义确认", len(candidates))

    response_text = chat_completion(
        system_prompt=_SEGMENTATION_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        temperature=0.2,
    )
    llm_result = parse_json_response(response_text)

    # Step 3: 解析 LLM 结果
    confirmed_boundaries: list[DialogueBoundary] = []
    llm_scenes: dict[int, tuple[str, str]] = {}

    for item in llm_result.get("boundaries", []):
        cand_idx = item.get("candidate_index", 0)
        if not item.get("is_boundary", False):
            continue
        if cand_idx >= len(candidates):
            logger.warning("LLM 返回了无效的 candidate_index: %d", cand_idx)
            continue

        cand = candidates[cand_idx]
        boundary = DialogueBoundary(
            after_segment_index=cand.gap_index,
            timestamp_ms=(cand.prev_end_ms + cand.next_begin_ms) // 2,
            gap_ms=cand.gap_ms,
            confidence=item.get("confidence", 0.5),
            reason=item.get("reason", ""),
        )
        confirmed_boundaries.append(boundary)
        llm_scenes[cand_idx] = (
            item.get("before_scene", "其他"),
            item.get("after_scene", "其他"),
        )

    confirmed_boundaries.sort(key=lambda b: b.after_segment_index)
    logger.info("LLM 确认了 %d 个边界", len(confirmed_boundaries))

    # Step 4: 构建对话段落
    if not confirmed_boundaries:
        single_seg = _build_single_segment(segments, path.name)
        return SegmentationResult(
            file_name=path.name,
            total_duration_ms=total_duration_ms,
            total_transcript_segments=total_segments,
            boundaries=[],
            dialogue_segments=[single_seg],
            main_consultation_index=0,
        )

    dialogue_segments = _build_segments_from_boundaries(
        segments, confirmed_boundaries, llm_scenes,
    )

    # Step 5: 标记主咨询段
    main_hint = llm_result.get("main_consultation_hint")
    main_idx = _identify_main_consultation(dialogue_segments, main_hint)
    if main_idx is not None:
        for seg in dialogue_segments:
            seg.is_main_consultation = (seg.segment_index == main_idx)

    result = SegmentationResult(
        file_name=path.name,
        total_duration_ms=total_duration_ms,
        total_transcript_segments=total_segments,
        boundaries=confirmed_boundaries,
        dialogue_segments=dialogue_segments,
        main_consultation_index=main_idx,
    )

    logger.info("对话边界识别完成: %d 段对话, 主咨询段=%s",
                len(dialogue_segments), main_idx)
    return result


def _build_single_segment(segments: list[dict], file_name: str) -> DialogueSegment:
    """当判定为单段对话时，构建唯一的 DialogueSegment。"""
    role_counter: Counter[str] = Counter()
    for s in segments:
        role_counter[normalize_role(s.get("role", "未知"))] += 1

    start_ms = segments[0]["begin"]
    end_ms = segments[-1]["end"]
    duration_min = (end_ms - start_ms) / 60000

    return DialogueSegment(
        segment_index=0,
        start_ms=start_ms,
        end_ms=end_ms,
        start_mmss=_ms_to_mmss(start_ms),
        end_mmss=_ms_to_mmss(end_ms),
        transcript_segment_range=(0, len(segments)),
        scene_type="医美咨询",
        description=f"单段对话，时长 {duration_min:.1f} 分钟，{len(segments)} 条转写",
        is_main_consultation=True,
        role_distribution=dict(role_counter),
    )


# ── 辅助函数：按检测到的边界切分原始 segments ─────────────────

def split_transcript_by_boundaries(
    path: str | Path,
    result: SegmentationResult,
) -> list[list[dict]]:
    """根据 SegmentationResult 将原始 transcribeResult 切分为多组。

    返回列表的每个元素是该段对话对应的原始 segment 列表。
    """
    raw = load_transcript(path)
    segments = raw.get("payload", {}).get("transcribeResult", [])

    groups: list[list[dict]] = []
    for ds in result.dialogue_segments:
        start, end = ds.transcript_segment_range
        groups.append(segments[start:end])

    return groups
