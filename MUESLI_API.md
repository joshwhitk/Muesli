# Muesli API

`muesli.py` exposes the recording, transcription, summarization, and session-browsing functionality as a Python API.

## Import

If you are using the repo directly:

```python
from muesli import Muesli
```

If you are embedding it from another project, add the repo directory to `sys.path` first.

## Quick Start

```python
from muesli import Muesli

m = Muesli()
m.start_recording()
session = m.stop_recording()
print(session["title"])
print(session["summary"])
```

## Main Entry Points

### Record from microphone

```python
m = Muesli()
m.start_recording()
session = m.stop_recording()
```

Returned `session` data includes fields such as:

```python
{
    "slug": "2026-04-22_10-30-00",
    "title": "Weekly Standup",
    "summary": "The team reviewed progress and open issues.",
    "transcript": "...",
    "speakers": 3,
    "duration": 323.5,
    "started_at": "2026-04-22T10:30:00",
    "audio_path": "C:/Users/<you>/Documents/MuesliData/analytics/audio/2026-04-22_10-30-00.mp3",
    "status": "done"
}
```

### Process an existing file

```python
session = m.process_file("example.wav")
```

### Transcribe only

```python
text = m.transcribe("example.wav")
```

### Summarize only

```python
info = m.summarize("Transcript text goes here.")
```

### Browse sessions

```python
sessions = m.list_sessions()
session = m.get_session("2026-04-22_10-30-00")
```

### Check audio paths

```python
path = m.audio_path("2026-04-22_10-30-00")
exists = m.audio_exists("2026-04-22_10-30-00")
```

## Optional Callback During Recording

You can receive live chunk updates while recording:

```python
def on_chunk(chunk_num, text_so_far):
    print(chunk_num, text_so_far)

m.start_recording(on_transcribed=on_chunk)
session = m.stop_recording()
```

## Resume Interrupted Sessions

If the app is interrupted during processing, you can resume unfinished sessions:

```python
def on_resume(slug, stage):
    print(slug, stage)

m = Muesli(auto_resume=True)
completed = m.resume_interrupted(on_progress=on_resume)
```

Typical progress stages include:

- `resuming`
- `transcribing`
- `summarising`
- `done`
- `no_audio`

## Runtime Notes

- Whisper is loaded lazily on first use.
- Summarization is also loaded lazily.
- `stop_recording()` is synchronous and waits for final processing to finish.
- Audio files are written to the configured shared directory.
- Session metadata is stored under `recordings/` in the repo directory by default.

## Configuration

The API reads `config.json` when present.

Common settings:

```json
{
  "shared_dir": "C:\\Users\\<you>\\Documents\\MuesliData\\analytics\\audio",
  "whisper_model": "large-v3",
  "whisper_device": "auto"
}
```

## Smoke Test

There is a simple smoke test script in the repo root:

```powershell
.venv\Scripts\python.exe test_muesli.py
```

or on Linux:

```bash
.venv/bin/python test_muesli.py
```
