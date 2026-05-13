"""分析 CLI。

用法:
    python -m smart_badge_api.analysis raw/example.json
    python -m smart_badge_api.analysis raw/ --output results/
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .production import analyze_transcript_for_production
from ..core.config import get_settings

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


def _result_path(output_dir: Path, source_path: Path) -> Path:
    return output_dir / f"{source_path.stem}.result.json"


def _print_summary(source_path: Path, result_dict: dict) -> None:
    evaluation = result_dict.get("consultation_evaluation", {})
    demands = result_dict.get("customer_demands", {})
    concerns = result_dict.get("customer_concerns", {})
    focus_areas = [item.get("area", "") for item in demands.get("focus_areas", []) if item.get("area")]
    print(f"[OK] {source_path.name}")
    print(f"  综合评分: {evaluation.get('overall_score', '-')}")
    print(f"  关注部位: {', '.join(focus_areas) if focus_areas else '无'}")
    print(f"  顾虑条数: {len(concerns.get('items', []))}")


def main() -> None:
    parser = argparse.ArgumentParser(description="执行录音分析并保存结果 JSON。")
    parser.add_argument("path", help="输入文件或目录")
    parser.add_argument(
        "--output",
        default=str(get_settings().results_path),
        help="结果输出目录，默认使用 settings.results_dir",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="仅输出错误，不打印每个文件的摘要",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    input_path = Path(args.path).resolve()
    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    files = _iter_input_files(input_path)
    if not files:
        print("没有找到可分析的 JSON 文件。")
        return

    failed = 0
    for source_path in files:
        try:
            result_dict = analyze_transcript_for_production(source_path)
            target = _result_path(output_dir, source_path)
            target.write_text(json.dumps(result_dict, ensure_ascii=False, indent=2), encoding="utf-8")
            if not args.quiet:
                _print_summary(source_path, result_dict)
                print(f"  输出文件: {target}")
        except Exception as exc:
            failed += 1
            logger.exception("分析失败：%s", source_path.name)
            print(f"[FAIL] {source_path.name}: {exc}")

    print(f"完成：共 {len(files)} 个文件，成功 {len(files) - failed}，失败 {failed}。")


if __name__ == "__main__":
    main()
