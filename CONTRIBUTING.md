# Contributing

This repo is currently maintained as a practical tool, so the most useful contributions are the ones that reduce ambiguity, improve reliability, or make local setup less fragile.

## Before You Change Things

- keep changes focused
- avoid committing local runtime data
- prefer small, reviewable commits
- preserve the local-first design unless there is a strong reason not to

## Development Setup

### Windows

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

You may also need system dependencies such as:

- `ffmpeg`
- `portaudio`

## What To Test

At minimum, run:

```powershell
.venv\Scripts\python.exe test_muesli.py
```

If that is too heavy for the change you made, at least run a syntax check on the touched files:

```powershell
python -m py_compile muesli.py muesli_gui.py muesli_hotkey.py muesli_batch_transcribe.py test_muesli.py
```

## Commit Scope

Good pull requests for this repo usually fall into one of these buckets:

- transcription quality improvements
- Windows setup or launcher fixes
- sidecar / batch processing reliability
- documentation cleanup
- test stabilization

## Files That Should Usually Stay Out Of Git

Do not commit local artifacts such as:

- `.venv/`
- `outputs/`
- `recordings/`
- `.tmp/`
- `config.json`
- model files under `models/`

## Style Notes

- stick to ASCII unless a file already clearly uses Unicode
- keep comments short and only where they add real value
- prefer explicit, boring behavior over clever abstractions
- when changing paths or workflow conventions, update the docs in the same change
