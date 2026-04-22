# Muesli API — for the Algorithm v4 integration

## Setup

Add granola to your Python path:
```python
sys.path.insert(0, "/home/algorithm/granola")
```

Then import:
```python
from muesli import Muesli
```

## Quick start

```python
m = Muesli()              # lazy init, no resume
m = Muesli(auto_resume=True)  # also resumes interrupted sessions on startup

# ── Record a meeting ──────────────────────────────────────────
m.start_recording()
# ... time passes, chunks transcribe in background ...
session = m.stop_recording()
# session = {
#   "slug": "weekly-team-standup",
#   "title": "Weekly Team Standup",
#   "summary": "The team discussed sprint progress...",
#   "transcript": "Okay let's get started...",
#   "speakers": 3,
#   "corrections": "...",
#   "bugs": "...",
#   "duration": 323.5,
#   "started_at": "2026-04-14T10:30:00",
#   "audio_path": "/srv/al/muesli/weekly-team-standup.mp3",
#   "status": "done"
# }

# ── Process an existing audio file ───────────────────────────
session = m.process_file("/path/to/audio.wav")
# same return shape as above

# ── Transcribe only (no summary) ─────────────────────────────
text = m.transcribe("/path/to/audio.wav")
# returns plain string

# ── Summarize a transcript ────────────────────────────────────
info = m.summarize("Okay let's get started. So where are we on the API...")
# returns {"title": "...", "summary": "...", "speakers": N, "corrections": "...", "bugs": "..."}

# ── Browse past sessions ──────────────────────────────────────
sessions = m.list_sessions()       # list of session dicts, newest first
session  = m.get_session("slug")   # single session dict or None

# ── Audio file access ─────────────────────────────────────────
path = m.audio_path("slug")   # "/srv/al/muesli/slug.mp3" (or .wav), or None
exists = m.audio_exists("slug")

# ── Live transcript callback (optional) ───────────────────────
def on_chunk(chunk_num, text_so_far):
    print(f"Chunk {chunk_num}: {text_so_far}")

m.start_recording(on_transcribed=on_chunk)
session = m.stop_recording()

# ── Resume interrupted sessions ──────────────────────────────
# If the app was killed mid-processing, audio is still on disk.
# resume_interrupted() finds those sessions and re-runs transcribe + summarize.
def on_resume(slug, stage):
    print(f"{slug}: {stage}")  # "resuming", "transcribing", "summarising", "done", "no_audio"

completed = m.resume_interrupted(on_progress=on_resume)
# returns list of completed session dicts

# Or just pass auto_resume=True to __init__ to do this automatically (no callback).
```

## Notes

- First call to `transcribe()` or `start_recording()` loads the Whisper model (~1s on SSD, uses ~1GB RAM).
- First call to `summarize()` or `process_file()` loads the local LLM (~2s, uses ~2.5GB RAM). Falls back to Claude API if no GGUF model present.
- `stop_recording()` blocks until all chunks are transcribed + summarized + audio is converted to MP3 and saved. Typical: 5-15s after a few-minute recording.
- Whisper transcription on CPU runs at roughly 1-2x realtime — a 30-minute recording takes 15-30 minutes to transcribe.
- Audio files live in the shared dir (default `/srv/al/muesli/`). JSON metadata lives in `/home/algorithm/granola/recordings/`.
- All methods are synchronous. For async use, call from a thread.
- The `session` dict is the same shape everywhere — it's the JSON that gets saved to `recordings/{slug}.json`, plus an `audio_path` key.
- On startup, the GUI app (`granola.py`) automatically resumes any interrupted sessions and shows animated progress in the status bar.

## Testing

Run the smoke test:
```bash
cd /home/algorithm/granola
/home/algorithm/venv/bin/python test_muesli.py
```

Tests all API methods: init, list/get sessions, audio access, transcribe (silent wav), summarize, process_file, and resume_interrupted. Takes ~1-2 minutes (model loading dominates).
