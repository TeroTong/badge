"""对话边界识别 CLI。"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .segmentation import MIN_GAP_MS, _find_candidate_boundaries, _ms_to_mmss, detect_boundaries
from .transcript import load_transcript, normalize_role

logger = logging.getLogger(__name__)


def _is_transcript_json(path: Path) -> bool:
    return path.suffix == ".json" and not path.name.endswith((".result.json", ".segmentation.json"))


def _iter_input_files(path: Path) -> list[Path]:
    if path.is_file():
        if not _is_transcript_json(path):
            raise ValueError(f"不支持的输入文件：{path.name}")
        return [path]
    if path.is_dir():
        return sorted(file for file in path.glob("*.json") if _is_transcript_json(file))
    raise FileNotFoundError(f"输入路径不存在：{path}")


def _print_candidate_stats(path: Path, min_gap_ms: int) -> None:
    raw = load_transcript(path)
    segments = raw.get("payload", {}).get("transcribeResult", [])
    if not segments:
        print(f"[SKIP] {path.name}: 无转写内容")
        return

    candidates = _find_candidate_boundaries(segments, min_gap_ms=min_gap_ms)
    print(f"[STATS] {path.name}")
    print(f"  总时长: {_ms_to_mmss(segments[-1]['end'])}")
    print(f"  片段数: {len(segments)}")
    print(f"  候选边界: {len(candidates)}")
    for idx, candidate in enumerate(candidates[:5]):
        before = candidate.context_before.splitlines()[-1] if candidate.context_before else ""
        after = candidate.context_after.splitlines()[0] if candidate.context_after else ""
        print(
            "  "
            f"[{idx}] gap={candidate.gap_ms / 1000:.0f}s "
            f"{_ms_to_mmss(candidate.prev_end_ms)} -> {_ms_to_mmss(candidate.next_begin_ms)}"
        )
        if before:
            print(f"      前: {before}")
        if after:
            print(f"      后: {after}")


def _describe_segment(segment) -> str:
    role_distribution = ", ".join(f"{normalize_role(role)}={count}" for role, count in segment.role_distribution.items())
    return (
        f"#{segment.segment_index} {segment.start_mmss}-{segment.end_mmss} "
        f"{segment.scene_type} 主段={segment.is_main_consultation} "
        f"角色分布[{role_distribution}]"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="执行对话边界识别并输出 segmentation JSON。")
    parser.add_argument("path", help="输入文件或目录")
    parser.add_argument(
        "--output",
        default="segmentation_results",
        help="结果输出目录，默认 segmentation_results/",
    )
    parser.add_argument(
        "--min-gap",
        type=int,
        default=MIN_GAP_MS // 1000,
        help="候选边界的最小静默时间，单位秒",
    )
    parser.add_argument(
        "--stats-only",
        action="store_true",
        help="仅输出候选边界统计，不调用 LLM",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    input_path = Path(args.path).resolve()
    output_dir = Path(args.output).resolve()
    min_gap_ms = args.min_gap * 1000

    files = _iter_input_files(input_path)
    if not files:
        print("没有找到可处理的 JSON 文件。")
        return

    if not args.stats_only:
        output_dir.mkdir(parents=True, exist_ok=True)

    failed = 0
    for path in files:
        try:
            if args.stats_only:
                _print_candidate_stats(path, min_gap_ms)
                continue

            result = detect_boundaries(path, min_gap_ms=min_gap_ms)
            target = output_dir / f"{path.stem}.segmentation.json"
            target.write_text(result.model_dump_json(indent=2), encoding="utf-8")
            print(f"[OK] {path.name} -> {target}")
            for segment in result.dialogue_segments:
                print(f"  {_describe_segment(segment)}")
        except Exception as exc:
            failed += 1
            logger.exception("边界识别失败：%s", path.name)
            print(f"[FAIL] {path.name}: {exc}")

    print(f"完成：共 {len(files)} 个文件，成功 {len(files) - failed}，失败 {failed}。")


if __name__ == "__main__":
    main()
