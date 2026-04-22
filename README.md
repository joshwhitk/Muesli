# Muesli

Muesli is a local-first audio recorder, transcription app, and batch transcription tool.

It can:

- record from your microphone
- transcribe speech locally with Whisper
- summarize transcripts with a local GGUF model or an optional API fallback
- process existing audio files in batch
- write sidecar `.txt` and `.silent` files for downstream tooling

The current repo is Windows-first, but the core Python code also runs on Linux with the right audio and model dependencies.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey)

## Why This Repo Exists

Muesli started as a local meeting recorder and evolved into a practical transcription tool for poor-quality real-world audio, shared audio folders, and sidecar-based workflows.

The repo currently includes:

- a desktop GUI recorder: `muesli_gui.py`
- a reusable Python API: `muesli.py`
- a batch transcriber for `2026-*` style audio drops: `muesli_batch_transcribe.py`
- a Windows global hotkey listener: `muesli_hotkey.py`

## Current Status

The project is usable, but it is still a working tool rather than a polished packaged product.

- Windows setup is the best-supported path right now.
- The batch transcription workflow is production-useful.
- The GUI and launcher path are functional, but still evolving.
- There is no installer package or release build in this repo yet.

## Features

- Local recording at 16 kHz mono
- Local transcription with `faster-whisper`
- Optional local summarization with `llama-cpp-python`
- Optional Anthropic fallback if you configure an API key yourself
- Batch processing of existing audio files
- Sidecar output convention:
  - `slug.txt` for detected speech
  - `slug.silent` for processed audio with no useful speech
- Windows global hotkey support for launch-and-record

## Quick Start

### Windows

```powershell
git clone https://github.com/joshwhitk/Muesli.git
cd Muesli
setup_windows.bat
```

That script:

- creates `.venv`
- installs Python dependencies
- checks for `ffmpeg`
- creates desktop and startup shortcuts

After setup, launch the app with the desktop shortcut or run:

```powershell
.venv\Scripts\python.exe muesli_gui.py
```

### Linux

Linux is manual setup at the moment:

```bash
git clone https://github.com/joshwhitk/Muesli.git
cd Muesli
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

You will usually also want:

```bash
sudo apt install ffmpeg portaudio19-dev
```

Then run:

```bash
python muesli_gui.py
```

## Batch Transcription Workflow

The batch transcriber is designed for shared audio directories and poor-quality recordings.

Typical convention:

```text
2026-04-15_18-21-46.wav    recording
2026-04-15_18-21-46.txt    transcript with speech
2026-04-15_18-21-46.silent processed, no useful speech
```

The local mirror under `outputs/sidecars/` is intentionally ignored by Git.

## Configuration

Runtime configuration lives in `config.json` and is not committed.

Common fields:

```json
{
  "shared_dir": "C:\\Users\\Josh\\Documents\\MuesliData\\analytics\\audio",
  "launch_hotkey": "Ctrl+Shift+`",
  "whisper_model": "large-v3",
  "whisper_device": "auto"
}
```

Notes:

- `shared_dir` controls where exported audio and sidecar files go.
- `launch_hotkey` is used by the Windows hotkey listener.
- `whisper_model` and `whisper_device` tune transcription quality and speed.

## Models

### Whisper

Whisper is provided by `faster-whisper`. The model is downloaded and cached automatically on first use.

### Local summarization model

If you want local summarization, place a GGUF model in `models/`.

Example options:

- Phi-3.1-mini Q4
- Mistral 7B Instruct Q4
- another small instruction-tuned GGUF that works with `llama-cpp-python`

If no local summary model is available, the code can fall back to Anthropic if you provide an API key in `config.json`.

## Windows Hotkey

Muesli ships with a small background listener on Windows so the launch-and-record shortcut works system-wide.

Current default:

```text
Ctrl+Shift+`
```

This is handled by `RegisterHotKey`, so it works across normal desktop apps and is not limited to Explorer shortcut hotkeys.

## Development

### Basic setup

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### Smoke tests

There is a basic smoke test script:

```powershell
.venv\Scripts\python.exe test_muesli.py
```

It is closer to an integration smoke test than a unit test suite.

### Syntax validation

This repo includes a lightweight GitHub Actions workflow that compiles the Python files to catch syntax errors on push and pull request.

## Repo Layout

```text
muesli.py                   core API
muesli_gui.py               desktop GUI
muesli_batch_transcribe.py  batch transcription tool
muesli_hotkey.py            Windows global hotkey listener
setup_windows.bat           Windows setup helper
MUESLI_API.md               API notes and examples
assets/                     icons and branding assets
models/                     local GGUF models (not committed)
recordings/                 local metadata/audio working files (not committed)
outputs/                    batch outputs and sidecar mirror (not committed)
```

## Known Gaps

- No packaged installer or signed release build yet
- No formal migration path for older Granola-era paths/configs
- Tests are still smoke-test oriented
- The GUI still has some Windows-specific rough edges

## Documentation

- API notes: [MUESLI_API.md](MUESLI_API.md)
- Contribution guide: [CONTRIBUTING.md](CONTRIBUTING.md)

## License

MIT. See [LICENSE](LICENSE).
