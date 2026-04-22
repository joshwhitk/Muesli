# Muesli

A fully local AI meeting recorder. Records audio, transcribes with Whisper, and summarises with a local LLM. No cloud services required.

![Python](https://img.shields.io/badge/python-3.10+-blue) ![License](https://img.shields.io/badge/license-MIT-green) ![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20Windows-lightgrey)

## What it does

1. **Record** meetings from your microphone
2. **Transcribe** in real-time using [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (runs locally on CPU)
3. **Summarise** with a local LLM ([Phi-3.1-mini](https://huggingface.co/microsoft/Phi-3-mini-4k-instruct)) — title, summary, speaker count, transcription corrections, and bug/issue extraction
4. **Browse** past recordings with playback, search, and one-click transcript copy

Everything runs on your machine. Audio never leaves your computer.

## Screenshot

```
┌──────────────────────────────────────────────────────────┐
│ ⬤  Muesli                    ✎ Prompt  ● Start Recording │
├──────────┬───────────────────────────────────────────────┤
│ SESSIONS │  SESSION                                      │
│          │  Weekly Team Standup                           │
│ ✓ Weekly │  Apr 12, 2026  ·  Today  ·  5:23  ·  3 spk   │
│ ✓ Debug  │                                               │
│ ✓ Test   │  ▶ ⏹  0:00 / 5:23  ████░░░░░░                │
│          │                                               │
│          │  SUMMARY                                      │
│          │  The team discussed sprint progress...         │
│          │                                               │
│          │  TRANSCRIPT 📋                                │
│          │  Okay let's get started. So where are we...   │
└──────────┴───────────────────────────────────────────────┘
```

## Install

### Linux (Ubuntu/Debian)

```bash
git clone https://github.com/YOUR_USERNAME/muesli.git
cd muesli
./install.sh
```

The install script will:
- Create a Python virtual environment
- Install all dependencies
- Download the Phi-3.1-mini LLM (~2.3 GB)
- Create a desktop launcher

### Windows

```
setup_windows.bat
```

### Manual install

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
mkdir -p models
# Download a GGUF model into models/
venv/bin/python3 granola.py
```

Any `.gguf` file placed in the `models/` directory will be auto-detected.

## Requirements

- **Python 3.10+**
- **PortAudio** (`sudo apt install portaudio19-dev`)
- **ffmpeg** (optional, for MP3 compression — `sudo apt install ffmpeg`)
- ~**4 GB RAM** free (Whisper base ~1 GB + Phi-3.1-mini ~2.5 GB)
- ~**3 GB disk** for the LLM model

## How it works

### Recording pipeline

Audio is captured at 16 kHz mono and split into **30-second chunks**. Each chunk is processed in a background thread while recording continues:

```
Microphone → 30s WAV chunk → Whisper transcribe → Local LLM summarise
                                    ↓                      ↓
                            Live transcript          Chunk summary
                            appears in UI            (cached)
```

When you press **Stop**:
1. Final chunk is transcribed and summarised
2. All chunk transcripts are merged
3. One fast LLM call merges the chunk summaries into a final title + summary
4. Audio is converted to MP3 and saved

### Files

Each recording produces:

| File | Location | Contents |
|------|----------|----------|
| `slug.json` | `recordings/` | Metadata (title, summary, transcript, timestamps) |
| `slug.mp3` | shared dir | Compressed audio |
| `slug.txt` | shared dir | Markdown: title, summary, corrections, bugs, transcript |

### Claude API fallback

If no local model is available (or it fails), Muesli falls back to the Claude API. Set your API key in `config.json`:

```json
{"api_key": "sk-ant-..."}
```

This is optional — the app works fully offline with just the local model.

## Customisation

### Summary prompt

Click **✎ Prompt** in the top bar to edit `prompt.txt` in your text editor. The prompt uses `{transcript}` as a placeholder. By default it asks for:

- **title** — 1-5 word session name
- **summary** — 2-4 sentence overview
- **speakers** — count of distinct speakers
- **corrections** — likely transcription errors
- **bugs** — software issues mentioned in the conversation

### Different LLM model

Drop any GGUF model into the `models/` directory. Muesli auto-detects the first `.gguf` file it finds. Recommended models:

| Model | Size | Speed | Quality |
|-------|------|-------|---------|
| Phi-3.1-mini Q4_K_M | 2.3 GB | Fast | Good |
| Mistral-7B-Instruct Q4_K_M | 4.4 GB | Medium | Excellent |
| TinyLlama-1.1B Q8 | 1.2 GB | Very fast | Basic |

### Shared network folder

By default, MP3 and TXT files are saved to `/srv/al/muesli` (Linux) or `\\192.168.4.47\al\muesli` (Windows). Change this in `config.json`:

```json
{"shared_dir": "/path/to/your/share"}
```

## Keyboard shortcut

To launch Muesli with a hotkey (e.g. numpad Enter) on GNOME:

```bash
gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings \
  "['/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/']"

dconf write /org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/name "'Muesli'"
dconf write /org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/command \
  "'/path/to/venv/bin/python3 /path/to/granola.py'"
dconf write /org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/binding "'KP_Enter'"
```

## Tech stack

- **UI**: Tkinter (dark theme)
- **Recording**: PyAudio (16 kHz mono)
- **Transcription**: [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (Whisper base, CPU, int8)
- **Summarisation**: [llama-cpp-python](https://github.com/abetlen/llama-cpp-python) (Phi-3.1-mini-4k-instruct)
- **Playback**: pygame.mixer
- **Audio conversion**: ffmpeg (WAV → MP3)

## License

MIT
