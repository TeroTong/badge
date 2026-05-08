"""本地结果查看器。

用法:
    python -m smart_badge_api.analysis.viewer
    python -m smart_badge_api.analysis.viewer results/
"""

from __future__ import annotations

import html
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from ..core.config import get_settings

logger = logging.getLogger(__name__)


def _load_result_files(results_dir: Path) -> list[Path]:
    return sorted(results_dir.glob("*.result.json"), reverse=True)


def _load_result(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _score(result: dict) -> str:
    value = result.get("consultation_evaluation", {}).get("overall_score")
    return "-" if value is None else f"{value:.1f}"


def _resolve_result_path(results_dir: Path, file_id: str) -> Path | None:
    if not file_id or any(separator in file_id for separator in ("/", "\\")):
        return None

    path = (results_dir / f"{file_id}.result.json").resolve()
    if not path.is_relative_to(results_dir.resolve()):
        return None
    return path


def _build_index_html(results_dir: Path) -> str:
    items: list[str] = []
    for path in _load_result_files(results_dir):
        result = _load_result(path)
        score = _score(result)
        file_id = html.escape(path.name.replace(".result.json", ""))
        focus_areas = result.get("customer_demands", {}).get("focus_areas", [])
        focus_text = ", ".join(item.get("area", "") for item in focus_areas if item.get("area")) or "无重点部位"
        items.append(
            "<li>"
            f"<a href=\"/view/{file_id}\">{file_id}</a>"
            f" <strong>{score}</strong>"
            f" <span>{html.escape(focus_text)}</span>"
            "</li>"
        )

    list_html = "\n".join(items) if items else "<li>暂无结果文件</li>"
    return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <title>智能工牌分析结果查看器</title>
    <style>
      body {{ font-family: Arial, sans-serif; max-width: 1080px; margin: 32px auto; padding: 0 16px; }}
      h1 {{ margin-bottom: 8px; }}
      ul {{ padding-left: 20px; }}
      li {{ margin: 8px 0; }}
      strong {{ display: inline-block; min-width: 48px; margin: 0 12px; }}
      span {{ color: #555; }}
    </style>
  </head>
  <body>
    <h1>朗姿智能工牌系统分析结果查看器</h1>
    <p>结果目录：{html.escape(str(results_dir))}</p>
    <ul>{list_html}</ul>
  </body>
</html>"""


def _build_detail_html(results_dir: Path, file_id: str) -> tuple[int, str]:
    path = _resolve_result_path(results_dir, file_id)
    if path is None or not path.exists():
        return 404, "<h1>404</h1><p>结果文件不存在。</p>"

    result = _load_result(path)
    pretty = html.escape(json.dumps(result, ensure_ascii=False, indent=2))
    score = html.escape(_score(result))
    return 200, f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <title>{html.escape(file_id)}</title>
    <style>
      body {{ font-family: Arial, sans-serif; max-width: 1080px; margin: 32px auto; padding: 0 16px; }}
      pre {{ background: #f5f5f5; padding: 16px; overflow: auto; border-radius: 8px; }}
      a {{ color: #1677ff; }}
    </style>
  </head>
  <body>
    <p><a href="/">返回列表</a></p>
    <h1>{html.escape(file_id)}</h1>
    <p>综合评分：{score}</p>
    <pre>{pretty}</pre>
  </body>
</html>"""


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="在本地启动分析结果查看器。")
    parser.add_argument(
        "results_dir",
        nargs="?",
        default=str(get_settings().results_path),
        help="结果目录，默认使用 settings.results_dir",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    results_dir = Path(args.results_dir).resolve()
    results_dir.mkdir(parents=True, exist_ok=True)

    class ViewerHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/":
                body = _build_index_html(results_dir).encode("utf-8")
                self.send_response(200)
            elif self.path.startswith("/view/"):
                file_id = unquote(self.path.removeprefix("/view/"))
                status, html_body = _build_detail_html(results_dir, file_id)
                body = html_body.encode("utf-8")
                self.send_response(status)
            else:
                body = b"<h1>404</h1>"
                self.send_response(404)

            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), format % args)

    server = ThreadingHTTPServer((args.host, args.port), ViewerHandler)
    print(f"Viewer running at http://{args.host}:{args.port}")
    print(f"Results dir: {results_dir}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping viewer...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
