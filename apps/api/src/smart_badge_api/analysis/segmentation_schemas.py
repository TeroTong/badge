"""对话边界识别的数据模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CandidateBoundary(BaseModel):
    """统计学预筛出的候选边界。"""

    gap_index: int = Field(..., description="间隙位于第 N 个 segment 之后")
    gap_ms: int = Field(..., description="间隙时长（毫秒）")
    prev_end_ms: int = Field(..., description="间隙前最后一条 segment 的 end")
    next_begin_ms: int = Field(..., description="间隙后第一条 segment 的 begin")
    context_before: str = Field(..., description="间隙前若干句对话文本")
    context_after: str = Field(..., description="间隙后若干句对话文本")


class DialogueBoundary(BaseModel):
    """经 LLM 确认的对话边界。"""

    after_segment_index: int = Field(..., description="边界位于第 N 个 segment 之后（0-based）")
    timestamp_ms: int = Field(..., description="边界时间点（毫秒），取前段 end 与后段 begin 的中点")
    gap_ms: int = Field(..., description="该边界处的静默时长（毫秒）")
    confidence: float = Field(..., ge=0, le=1, description="置信度 0-1")
    reason: str = Field(..., description="判定为边界的理由")


class DialogueSegment(BaseModel):
    """识别出的单段连续对话。"""

    segment_index: int = Field(..., description="段落序号（0-based）")
    start_ms: int = Field(..., description="起始时间（毫秒）")
    end_ms: int = Field(..., description="结束时间（毫秒）")
    start_mmss: str = Field(..., description="起始时间 MM:SS 格式")
    end_mmss: str = Field(..., description="结束时间 MM:SS 格式")
    transcript_segment_range: tuple[int, int] = Field(
        ..., description="该段对应的原始 transcribeResult 索引范围 [start, end)",
    )
    scene_type: str = Field(
        ...,
        description="场景类型：医美咨询 / 前台接待 / 候诊闲聊 / 内部协作 / 其他",
    )
    description: str = Field(..., description="该段对话的简要描述")
    is_main_consultation: bool = Field(
        default=False,
        description="是否为主咨询段落",
    )
    role_distribution: dict[str, int] = Field(
        default_factory=dict,
        description="角色发言条数分布，如 {'咨询师': 120, '客户': 80}",
    )


class SegmentationResult(BaseModel):
    """对话边界识别的完整结果。"""

    file_name: str = Field(..., description="源文件名")
    total_duration_ms: int = Field(..., description="录音总时长（毫秒）")
    total_transcript_segments: int = Field(..., description="转写 segment 总数")
    boundaries: list[DialogueBoundary] = Field(
        default_factory=list,
        description="检测到的对话边界列表",
    )
    dialogue_segments: list[DialogueSegment] = Field(
        default_factory=list,
        description="划分出的对话段落列表",
    )
    main_consultation_index: int | None = Field(
        default=None,
        description="主咨询段落的 segment_index（若识别到多段，指向最主要的那段）",
    )
