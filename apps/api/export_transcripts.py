"""Export ASR transcription for unique recordings using faster-whisper."""
import json
import os
import time
from collections import defaultdict

RECORDINGS_DIR = "uploads/recordings"
OUTPUT_DIR = "transcripts_export"

os.chdir(os.path.dirname(os.path.abspath(__file__)))

# Find unique recordings by file size (duplicates from batch import testing)
files = []
for f in os.listdir(RECORDINGS_DIR):
    if f.endswith(".mp3"):
        fp = os.path.join(RECORDINGS_DIR, f)
        sz = os.path.getsize(fp)
        if sz > 10000:  # skip test dummies
            files.append((sz, f, fp))

by_size = defaultdict(list)
for sz, name, fp in files:
    by_size[sz].append((name, fp))

unique = []
for sz in sorted(by_size.keys(), reverse=True):
    copies = by_size[sz]
    copies.sort()
    unique.append(copies[0])

print(f"Will transcribe {len(unique)} unique recordings")
for name, fp in unique:
    print(f"  {name} ({os.path.getsize(fp)/1024/1024:.1f}MB)")

# Load model
print("\nLoading faster-whisper model (large-v3)...")
from faster_whisper import WhisperModel  # noqa: E402

model = WhisperModel("large-v3", device="cuda", compute_type="int8_float16")
print("Model loaded!\n")

os.makedirs(OUTPUT_DIR, exist_ok=True)

total_start = time.time()
for i, (name, fp) in enumerate(unique):
    base = name.replace(".mp3", "")
    print(f"[{i+1}/{len(unique)}] Transcribing {name}...")
    t0 = time.time()

    segments, info = model.transcribe(fp, language="zh", beam_size=5, vad_filter=True)

    all_segments = []
    full_text_parts = []
    for seg in segments:
        all_segments.append({
            "start": round(seg.start, 2),
            "end": round(seg.end, 2),
            "text": seg.text.strip(),
        })
        full_text_parts.append(seg.text.strip())

    elapsed = time.time() - t0
    duration = info.duration
    rtf = elapsed / duration if duration > 0 else 0

    # Save text file
    with open(os.path.join(OUTPUT_DIR, f"{base}.txt"), "w", encoding="utf-8") as f:
        f.write(f"文件: {name}\n")
        f.write(f"时长: {duration:.1f}秒 ({duration/60:.1f}分钟)\n")
        f.write(f"转写耗时: {elapsed:.1f}秒\n")
        f.write(f"段落数: {len(all_segments)}\n")
        f.write("=" * 60 + "\n\n")
        for seg in all_segments:
            mins = int(seg["start"] // 60)
            secs = seg["start"] % 60
            f.write(f"[{mins:02d}:{secs:05.2f}] {seg['text']}\n")
        f.write("\n" + "=" * 60 + "\n完整文本:\n")
        f.write("\n".join(full_text_parts))

    # Save JSON
    with open(os.path.join(OUTPUT_DIR, f"{base}.json"), "w", encoding="utf-8") as f:
        json.dump(
            {
                "file": name,
                "duration_seconds": round(duration, 2),
                "transcription_seconds": round(elapsed, 2),
                "segment_count": len(all_segments),
                "segments": all_segments,
                "full_text": "\n".join(full_text_parts),
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(f"  Done: {duration:.0f}s audio in {elapsed:.0f}s ({1/rtf:.1f}x realtime), {len(all_segments)} segments")

total_elapsed = time.time() - total_start
print("\n===== ALL DONE =====")
print(f"Total time: {total_elapsed:.0f}s ({total_elapsed/60:.1f} min)")
print(f"Output directory: {OUTPUT_DIR}/")
