"""Batch-transcribe 2026 WAV files and write sidecar markers in place."""

from __future__ import annotations

import argparse
import ctypes
import csv
import os
import time
from pathlib import Path
import wave

from faster_whisper import WhisperModel

DEFAULT_INPUT = Path(r"\\ALGORITHM-INSPIRON-3537\algorithm\analytics\audio")
PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = PROJECT_DIR / "outputs"


def cuda_runtime_available() -> bool:
    if os.name != "nt":
        return True
    try:
        ctypes.WinDLL("cublas64_12.dll")
        return True
    except OSError:
        return False


def sidecar_path(base_dir: Path, wav_path: Path, suffix: str) -> Path:
    if base_dir == wav_path.parent:
        return wav_path.with_suffix(suffix)
    return base_dir / f"{wav_path.stem}{suffix}"


def get_duration_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as wav_file:
            return wav_file.getnframes() / float(wav_file.getframerate())
    except FileNotFoundError:
        return 0.0


def build_model() -> WhisperModel:
    candidates = []
    if cuda_runtime_available():
        candidates.append(("large-v3", "cuda", "float16"))
    candidates.extend(
        [
            ("large-v3", "cpu", "int8"),
            ("medium", "cpu", "int8"),
        ]
    )
    last_error = None
    for model_name, device, compute_type in candidates:
        try:
            return WhisperModel(model_name, device=device, compute_type=compute_type)
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Unable to initialize Whisper: {last_error}")


def read_existing_result(path: Path, preferred_sidecar_dir: Path, fallback_sidecar_dir: Path) -> dict | None:
    for label, base_dir in (
        ("share", preferred_sidecar_dir),
        ("local", fallback_sidecar_dir),
    ):
        transcript_path = sidecar_path(base_dir, path, ".txt")
        silent_path = sidecar_path(base_dir, path, ".silent")

        if transcript_path.exists():
            transcript = transcript_path.read_text(encoding="utf-8", errors="replace").strip()
            return {
                "language": "",
                "language_probability": 0.0,
                "speech_seconds": 0.0,
                "segment_count": 0,
                "has_speech": bool(transcript),
                "transcript": transcript,
                "status": f"existing_txt_{label}",
                "sidecar_dir": str(base_dir),
            }

        if silent_path.exists():
            return {
                "language": "",
                "language_probability": 0.0,
                "speech_seconds": 0.0,
                "segment_count": 0,
                "has_speech": False,
                "transcript": "",
                "status": f"existing_silent_{label}",
                "sidecar_dir": str(base_dir),
            }

    return None


def transcribe_file(model: WhisperModel, path: Path) -> dict:
    segments, info = model.transcribe(
        str(path),
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 700},
        condition_on_previous_text=False,
    )

    parts = []
    speech_seconds = 0.0
    segment_count = 0
    for segment in segments:
        text = segment.text.strip()
        if text:
            parts.append(text)
            speech_seconds += max(0.0, float(segment.end) - float(segment.start))
            segment_count += 1

    transcript = " ".join(parts).strip()
    has_speech = bool(transcript) and (speech_seconds >= 1.0 or segment_count >= 2)
    return {
        "language": getattr(info, "language", "") or "",
        "language_probability": round(float(getattr(info, "language_probability", 0.0) or 0.0), 4),
        "speech_seconds": round(speech_seconds, 3),
        "segment_count": segment_count,
        "has_speech": has_speech,
        "transcript": transcript,
        "status": "new",
        "sidecar_dir": "",
    }


def write_sidecars(path: Path, result: dict, preferred_sidecar_dir: Path, fallback_sidecar_dir: Path) -> None:
    for label, base_dir in (
        ("share", preferred_sidecar_dir),
        ("local", fallback_sidecar_dir),
    ):
        transcript_path = sidecar_path(base_dir, path, ".txt")
        silent_path = sidecar_path(base_dir, path, ".silent")
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if result["has_speech"]:
                transcript_path.write_text(result["transcript"] + "\n", encoding="utf-8")
                if silent_path.exists():
                    silent_path.unlink()
            else:
                silent_path.write_text("", encoding="utf-8")
                if transcript_path.exists():
                    transcript_path.unlink()
            result["sidecar_dir"] = str(base_dir)
            result["status"] = f"{result['status']}_{label}"
            return
        except PermissionError:
            continue
    raise PermissionError(f"Could not write sidecars for {path.name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--glob", default="2026-*.wav")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    input_dir = args.input_dir
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    local_sidecar_dir = output_dir / "sidecars"
    local_sidecar_dir.mkdir(parents=True, exist_ok=True)

    stems = {path.stem for path in input_dir.glob(args.glob)}
    for base_dir in (input_dir, local_sidecar_dir):
        for pattern in ("2026-*.txt", "2026-*.silent"):
            stems.update(path.stem for path in base_dir.glob(pattern))

    files = [input_dir / f"{stem}.wav" for stem in sorted(stems)]
    if not files:
        raise SystemExit(f"No WAV or sidecar files found for 2026-* in {input_dir}")

    model = None
    all_rows = []
    speech_rows = []
    started = time.time()

    for index, path in enumerate(files, start=1):
        result = read_existing_result(path, input_dir, local_sidecar_dir)
        if not path.exists():
            if result is None:
                print(f"[{index}/{len(files)}] {path.name} status=missing_no_sidecar speech=False segments=0 elapsed={time.time() - started:.1f}s")
                continue
            duration_seconds = 0.0
        else:
            duration_seconds = get_duration_seconds(path)

        if result is None:
            if model is None:
                model = build_model()
            try:
                result = transcribe_file(model, path)
                write_sidecars(path, result, input_dir, local_sidecar_dir)
            except FileNotFoundError:
                result = read_existing_result(path, input_dir, local_sidecar_dir)
                if result is None:
                    print(f"[{index}/{len(files)}] {path.name} status=missing_during_processing speech=False segments=0 elapsed={time.time() - started:.1f}s")
                    continue
        row = {
            "filename": path.name,
            "duration_seconds": round(duration_seconds, 3),
            **result,
        }
        all_rows.append(row)
        if result["has_speech"]:
            speech_rows.append(row)

        elapsed = time.time() - started
        print(
            f"[{index}/{len(files)}] {path.name} "
            f"status={result['status']} "
            f"speech={result['has_speech']} "
            f"segments={result['segment_count']} "
            f"elapsed={elapsed:.1f}s"
        )

    fieldnames = [
        "filename",
        "duration_seconds",
        "language",
        "language_probability",
        "speech_seconds",
        "segment_count",
        "has_speech",
        "status",
        "sidecar_dir",
        "transcript",
    ]
    all_csv = output_dir / "all_2026_wav_transcripts.csv"
    speech_csv = output_dir / "speech_2026_wav_files.csv"

    with all_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)

    with speech_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(speech_rows)

    print(f"Wrote {all_csv}")
    print(f"Wrote {speech_csv}")
    print(f"Share sidecars preferred at {input_dir}")
    print(f"Local sidecar mirror at {local_sidecar_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
