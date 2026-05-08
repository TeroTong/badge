from __future__ import annotations

import argparse
import asyncio
import json

from smart_badge_api.dingtalk_audio_backlog import sync_dingtalk_audio_archive_backlog


async def main() -> None:
    parser = argparse.ArgumentParser(description="Process archived DingTalk audio backlog into the ASR/LLM pipeline.")
    parser.add_argument("--workers", type=int, default=3, help="Parallel pipeline workers. Default: 3")
    parser.add_argument("--limit", type=int, default=0, help="Only process the first N pending archive items.")
    parser.add_argument("--sn", action="append", dest="sns", help="Only process specific badge SN values.")
    parser.add_argument(
        "--no-retry-failed",
        action="store_true",
        help="Skip manifests currently marked as failed.",
    )
    args = parser.parse_args()

    result = await sync_dingtalk_audio_archive_backlog(
        workers=args.workers,
        limit=args.limit,
        sns=args.sns,
        retry_failed=not args.no_retry_failed,
    )
    print(
        json.dumps(
            {
                "archiveItems": result.archive_items,
                "stagedNew": result.staged_new,
                "alreadyStaged": result.already_staged,
                "processedNow": result.processed_now,
                "processSummary": result.process_summary,
                "finalArchiveStatus": result.final_archive_status,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
