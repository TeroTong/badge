from __future__ import annotations

import argparse
import json
from pathlib import Path

from smart_badge_api.asr.sensevoice_3dspeaker_provider import transcribe_audio


def main() -> None:
    parser = argparse.ArgumentParser(description="Run SenseVoice + 3D-Speaker on a single audio file.")
    parser.add_argument("audio", help="Audio file path")
    parser.add_argument("--output", help="Optional JSON output path")
    args = parser.parse_args()

    audio_path = Path(args.audio).expanduser().resolve()
    utterances = transcribe_audio(audio_path)
    payload = {
        "audioPath": str(audio_path),
        "utteranceCount": len(utterances),
        "utterances": utterances,
    }

    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
