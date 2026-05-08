import argparse
import asyncio
import json
from datetime import datetime

from zoneinfo import ZoneInfo

from smart_badge_api.dingtalk_audio_archive import archive_audio_files, get_archive_root

TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")

async def main() -> None:
    parser = argparse.ArgumentParser(description="Download all DingTalk badge audio files into archive storage.")
    parser.add_argument("--sn", action="append", dest="sns", help="Only download specific SN. Can repeat.")
    parser.add_argument("--workers", type=int, default=4, help="Parallel download workers. Default: 4")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing archived files.")
    args = parser.parse_args()

    archive_root = get_archive_root()
    result = await archive_audio_files(
        sns=args.sns,
        overwrite=args.overwrite,
        workers=args.workers,
    )

    summary = {
        "generatedAt": datetime.now(TZ_SHANGHAI).isoformat(),
        "deviceCount": len({item.sn for item in result.items}),
        "totalFiles": len(result.items),
        "downloaded": result.downloaded,
        "skipped": result.skipped,
        "failed": result.failed,
        "items": [
            {
                "sn": item.sn,
                "fileId": item.file_id,
                "status": item.status,
                "savedPath": str(item.saved_path) if item.saved_path else None,
                "message": item.message,
            }
            for item in result.items
        ],
    }
    summary_path = archive_root / "download_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"summary={summary_path}")
    print(f"downloaded={result.downloaded} skipped={result.skipped} failed={result.failed}")


if __name__ == "__main__":
    asyncio.run(main())
