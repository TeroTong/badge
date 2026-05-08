from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import soundfile as sf

DEFAULT_MODELS = [
    "sensevoice_3dspeaker",
    "high_precision_3dspeaker",
    "fun_asr_nano",
    "paraformer_zh_hotword",
]

MODEL_CATALOG: dict[str, dict[str, Any]] = {
    "sensevoice_3dspeaker": {
        "kind": "provider",
    },
    "high_precision_3dspeaker": {
        "kind": "provider",
    },
    "fun_asr_nano": {
        "kind": "funasr",
        "model": "FunAudioLLM/Fun-ASR-Nano-2512",
        "vad_model": "fsmn-vad",
        "batch_size_s": 0,
    },
    "paraformer_zh_hotword": {
        "kind": "funasr",
        "model": "paraformer-zh",
        "vad_model": "fsmn-vad",
        "punc_model": "ct-punc",
        "batch_size_s": 300,
        "supports_hotword": True,
    },
    "whisper_large_v3": {
        "kind": "funasr",
        "model": "Whisper-large-v3",
        "batch_size_s": 60,
    },
    "whisper_large_v3_turbo": {
        "kind": "funasr",
        "model": "Whisper-large-v3-turbo",
        "batch_size_s": 60,
    },
}

_FUNASR_MODELS: dict[tuple[str, str], Any] = {}


def _resolve_device_label(value: str) -> str:
    if value != "auto":
        return value
    try:
        import torch
    except Exception:
        return "cpu"
    return "cuda:0" if torch.cuda.is_available() else "cpu"


def _load_hotwords(path: Path | None) -> list[str]:
    if path is None or not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _audio_duration_seconds(audio_path: Path) -> float:
    with sf.SoundFile(str(audio_path)) as handle:
        return float(len(handle) / handle.samplerate)


def _domain_hits(text: str, hotwords: list[str]) -> list[str]:
    return [term for term in hotwords if term in text]


def _load_funasr_model(model_key: str, device: str) -> Any:
    cache_key = (model_key, device)
    if cache_key in _FUNASR_MODELS:
        return _FUNASR_MODELS[cache_key]

    from funasr import AutoModel

    config = MODEL_CATALOG[model_key]
    kwargs: dict[str, Any] = {
        "model": config["model"],
        "device": device,
    }
    if config.get("vad_model"):
        kwargs["vad_model"] = config["vad_model"]
        kwargs["vad_kwargs"] = {"max_single_segment_time": 30000}
    if config.get("punc_model"):
        kwargs["punc_model"] = config["punc_model"]

    model = AutoModel(**kwargs)
    _FUNASR_MODELS[cache_key] = model
    return model


def _normalize_funasr_text(payload: Any) -> tuple[str, int]:
    if not isinstance(payload, list) or not payload:
        return "", 0
    item = payload[0] if isinstance(payload[0], dict) else {}
    text = str(item.get("text") or "").strip()
    timestamp_count = len(item.get("timestamp") or []) if isinstance(item, dict) else 0
    return text, timestamp_count


def _run_provider_model(audio_path: Path) -> dict[str, Any]:
    started_at = time.perf_counter()
    from smart_badge_api.asr.sensevoice_3dspeaker_provider import transcribe_audio

    utterances = transcribe_audio(audio_path)
    elapsed = time.perf_counter() - started_at
    transcript = "".join(str(item.get("text") or "") for item in utterances)
    speakers = sorted({str(item.get("speaker_id") or "unknown") for item in utterances})
    return {
        "elapsedSeconds": round(elapsed, 3),
        "text": transcript,
        "utteranceCount": len(utterances),
        "speakerCount": len(speakers),
        "speakers": speakers,
        "timestampCount": 0,
    }


def _run_high_precision_provider_model(audio_path: Path) -> dict[str, Any]:
    started_at = time.perf_counter()
    from smart_badge_api.asr.high_precision_3dspeaker_provider import transcribe_audio

    utterances = transcribe_audio(audio_path)
    elapsed = time.perf_counter() - started_at
    transcript = "".join(str(item.get("text") or "") for item in utterances)
    speakers = sorted({str(item.get("speaker_id") or "unknown") for item in utterances})
    return {
        "elapsedSeconds": round(elapsed, 3),
        "text": transcript,
        "utteranceCount": len(utterances),
        "speakerCount": len(speakers),
        "speakers": speakers,
        "timestampCount": 0,
    }


def _run_funasr_model(
    model_key: str,
    audio_path: Path,
    *,
    device: str,
    hotwords: list[str],
) -> dict[str, Any]:
    config = MODEL_CATALOG[model_key]
    model = _load_funasr_model(model_key, device)
    generate_kwargs: dict[str, Any] = {
        "input": str(audio_path),
        "cache": {},
    }
    batch_size_s = config.get("batch_size_s")
    if batch_size_s is not None:
        generate_kwargs["batch_size_s"] = batch_size_s
    if config.get("supports_hotword") and hotwords:
        generate_kwargs["hotword"] = " ".join(hotwords)

    started_at = time.perf_counter()
    payload = model.generate(**generate_kwargs)
    elapsed = time.perf_counter() - started_at
    text, timestamp_count = _normalize_funasr_text(payload)
    return {
        "elapsedSeconds": round(elapsed, 3),
        "text": text,
        "utteranceCount": 0,
        "speakerCount": 0,
        "speakers": [],
        "timestampCount": timestamp_count,
    }


def _run_single_model(
    model_key: str,
    audio_path: Path,
    *,
    device: str,
    hotwords: list[str],
) -> dict[str, Any]:
    if model_key not in MODEL_CATALOG:
        raise ValueError(f"Unsupported model key: {model_key}")

    if model_key == "sensevoice_3dspeaker":
        result = _run_provider_model(audio_path)
    elif model_key == "high_precision_3dspeaker":
        result = _run_high_precision_provider_model(audio_path)
    elif MODEL_CATALOG[model_key]["kind"] == "provider":
        raise ValueError(f"Unsupported provider model key: {model_key}")
    else:
        result = _run_funasr_model(model_key, audio_path, device=device, hotwords=hotwords)

    text = str(result["text"] or "").strip()
    result["charCount"] = len(text.replace(" ", ""))
    result["preview"] = text[:200]
    result["domainTermHits"] = _domain_hits(text, hotwords)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark multiple free ASR models on archived audio.")
    parser.add_argument("audio", nargs="+", help="Audio file paths to benchmark")
    parser.add_argument(
        "--models",
        nargs="+",
        default=DEFAULT_MODELS,
        choices=sorted(MODEL_CATALOG.keys()),
        help="Model keys to benchmark",
    )
    parser.add_argument("--device", default="auto", help="Inference device, e.g. auto/cpu/cuda:0")
    parser.add_argument("--output", help="Optional JSON output path")
    parser.add_argument(
        "--hotword-file",
        default=str(Path(__file__).with_name("asr_hotwords_default.txt")),
        help="Optional hotword file for models that support it",
    )
    args = parser.parse_args()

    device = _resolve_device_label(args.device)
    hotword_file = Path(args.hotword_file).expanduser().resolve() if args.hotword_file else None
    hotwords = _load_hotwords(hotword_file)

    payload: dict[str, Any] = {
        "device": device,
        "models": args.models,
        "hotwordFile": str(hotword_file) if hotword_file else None,
        "hotwordCount": len(hotwords),
        "results": [],
    }

    for audio_value in args.audio:
        audio_path = Path(audio_value).expanduser().resolve()
        audio_result: dict[str, Any] = {
            "audioPath": str(audio_path),
            "durationSeconds": round(_audio_duration_seconds(audio_path), 3),
            "models": {},
        }
        for model_key in args.models:
            audio_result["models"][model_key] = _run_single_model(
                model_key,
                audio_path,
                device=device,
                hotwords=hotwords,
            )
        payload["results"].append(audio_result)

    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
