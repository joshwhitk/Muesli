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

## Likely MCP / Local Service Mapping

The current Python API is also the natural base for a future local MCP or app-service layer.

Core operations map roughly like this:

- `record_default_mic`
  - start a recording from the default microphone and finalize it into a local voice note
- `transcribe_file`
  - accept an audio file path and return transcript text without necessarily saving a note
- `ingest_voice_note`
  - copy or normalize an audio file into the managed Muesli store and process it like a locally recorded note
- `list_notes` / `get_note`
  - browse saved notes and retrieve fields such as title, summary, transcript, started-at timestamp, duration, speakers, slug, and audio path
- `search_notes_deterministic`
  - plain deterministic search over saved fields
- `search_notes_llm`
  - semantic or prompt-based search over transcript / summary / title
- `regenerate_component`
  - re-run summary or title generation from an existing transcript or audio-backed note
- `overwrite_component`
  - save corrected title / summary / transcript text back into the local note
- `delete_note`
  - remove a managed note and its owned local artifacts

Useful component-level targets:

- `audio`
- `transcript`
- `summary`
- `title`
- `started_at`
- `duration`
- `speakers`
- `status`

Useful metadata or control actions beyond the current desktop UI:

- `get_component_size`
- `get_component_length`
- `get_note_status`
- `reprocess_from_audio`
- `export_note`

What that future service layer should still add explicitly:

- health / capability introspection
- backend selection without storing secrets in shared registries
- idempotent job submission and job IDs for long-running processing
- provenance fields so downstream apps know whether title/summary/transcript were user-edited, AI-generated, or regenerated later

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
