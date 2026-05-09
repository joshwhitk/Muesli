#!/usr/bin/env python3
"""Quick smoke test for the muesli module API."""

import atexit, json, shutil, subprocess, sys, os, wave, struct, tempfile, time, types, urllib.request, urllib.error, socket, threading
# Unbuffered stdout so progress appears immediately when piped
sys.stdout.reconfigure(line_buffering=True)
REPO_DIR = os.path.dirname(os.path.abspath(__file__))
TEST_HOME = tempfile.mkdtemp(prefix="muesli-test-home-")
TEST_SHARED_DIR = os.path.join(TEST_HOME, "shared_audio")
os.makedirs(TEST_SHARED_DIR, exist_ok=True)
with open(os.path.join(TEST_HOME, "config.json"), "w", encoding="utf-8") as _cfg:
    json.dump({"shared_dir": TEST_SHARED_DIR}, _cfg)
os.environ["MUESLI_HOME"] = TEST_HOME
# Isolate the runtime state file (pause flag, app_pid, etc.) from the real
# user state — required by [17e] which round-trips set_processing_paused.
# Must be set BEFORE the muesli imports below so paths.py picks it up.
os.environ["MUESLI_RUNTIME_DIR"] = os.path.join(TEST_HOME, "runtime")
os.makedirs(os.environ["MUESLI_RUNTIME_DIR"], exist_ok=True)
atexit.register(lambda: shutil.rmtree(TEST_HOME, ignore_errors=True))
sys.path.insert(0, REPO_DIR)

import muesli as muesli_module
import muesli_gui as muesli_gui_module
import muesli_service as muesli_service_module
from muesli import Muesli

PASS = 0
FAIL = 0

FAKE_LLM_JSON = json.dumps({
    "title": "Test Session",
    "summary": "The transcript was summarized successfully.",
    "speakers": 1,
    "corrections": "",
    "bugs": "",
})
muesli_module._llm_generate = lambda prompt_text: FAKE_LLM_JSON

def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  OK  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def make_silent_wav(path, duration_s=3):
    """Generate a short silent WAV for testing."""
    rate = 16000
    n = int(rate * duration_s)
    wf = wave.open(path, "wb")
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(rate)
    wf.writeframes(struct.pack(f"<{n}h", *([0] * n)))
    wf.close()


class FakeInputBlock:
    def __init__(self, payload):
        self._payload = payload

    def copy(self):
        return self

    def tobytes(self):
        return self._payload


class SubmitCollector:
    def __init__(self):
        self.paths = []

    def submit(self, path):
        self.paths.append(path)


print("=== muesli module test ===\n")

# ── 1. Init ──────────────────────────────────────────────────────────────────
print("[1] Init")
m = Muesli()
check("Muesli() creates instance", m is not None)
check("test harness isolates app home from the live repo",
      os.path.abspath(muesli_module.APP_DIR) == os.path.abspath(TEST_HOME),
      f"APP_DIR={muesli_module.APP_DIR!r}")
check("test harness isolates shared audio output",
      os.path.abspath(m._shared_dir) == os.path.abspath(TEST_SHARED_DIR),
      f"shared_dir={m._shared_dir!r}")
check("shared_dir exists", os.path.isdir(m._shared_dir))

# ── 2. list_sessions ─────────────────────────────────────────────────────────
print("\n[2] list_sessions")
sessions = m.list_sessions()
check("list_sessions returns list", isinstance(sessions, list))
check("sessions have expected keys", all(
    "slug" in s and "status" in s and "audio_path" in s
    for s in sessions
), f"got {len(sessions)} sessions")
if sessions:
    s = sessions[0]
    print(f"       newest: {s.get('title', s.get('slug'))} [{s['status']}]")

# ── 3. get_session ───────────────────────────────────────────────────────────
print("\n[3] get_session")
done = [s for s in sessions if s["status"] == "done"]
if done:
    slug = done[0]["slug"]
    s = m.get_session(slug)
    check("get_session returns dict", isinstance(s, dict))
    check("get_session has audio_path", "audio_path" in s)
    check("audio_path exists or is None",
          s["audio_path"] is None or os.path.exists(s["audio_path"]))
else:
    print("  SKIP  no done sessions to test get_session")

# ── 4. audio_path / audio_exists ─────────────────────────────────────────────
print("\n[4] audio_path / audio_exists")
if done:
    slug = done[0]["slug"]
    path = m.audio_path(slug)
    check("audio_path returns string", isinstance(path, str))
    check("audio_path file exists", os.path.exists(path))
    check("audio_exists agrees", m.audio_exists(slug))
check("audio_path returns None for bogus slug", m.audio_path("no-such-slug-999") is None)
check("audio_exists returns False for bogus slug", not m.audio_exists("no-such-slug-999"))

# ── 5. transcribe (silent wav) ───────────────────────────────────────────────
print("\n[5] transcribe")
with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
    tmp_wav = f.name
try:
    make_silent_wav(tmp_wav, duration_s=2)
    text = m.transcribe(tmp_wav)
    check("transcribe returns string", isinstance(text, str))
    check("transcribe of silence is short", len(text) < 200,
          f"got {len(text)} chars: {text[:80]!r}")
finally:
    os.unlink(tmp_wav)

# ── 6. summarize ─────────────────────────────────────────────────────────────
print("\n[6] summarize")
info = m.summarize("Hey team, the login page is broken on Safari. Let's fix it by Thursday.")
check("summarize returns dict", isinstance(info, dict))
check("summarize has title", "title" in info, f"keys: {list(info.keys())}")
check("summarize has summary", "summary" in info)
check("summarize has speakers", "speakers" in info)
print(f"       title: {info.get('title')}")
print(f"       summary: {info.get('summary', '')[:120]}")

# ── 6b. summary modes + re-summarize ─────────────────────────────────────────
print("\n[6b] summary modes + re-summarize")
cfg = muesli_module._load_config()
check("config normalization adds summary modes",
      isinstance(cfg.get("summary_modes"), list) and len(cfg.get("summary_modes")) >= 5,
      f"summary_modes={cfg.get('summary_modes')!r}")
check("config normalization keeps audio input settings fields",
      "audio_input_backend" in cfg and "audio_input_device" in cfg,
      f"config keys={list(cfg.keys())}")
check("config normalization keeps transcription backend settings fields",
      "transcription_backend" in cfg and "openai_transcription_model" in cfg and "openai_api_key" in cfg,
      f"config keys={list(cfg.keys())}")
check("config normalization defaults to local transcription",
      cfg.get("transcription_backend") == "local",
      f"transcription_backend={cfg.get('transcription_backend')!r}")
check("config normalization keeps a general summary mode",
      any(mode.get("id") == "general" for mode in cfg.get("summary_modes", [])))
check("config normalization sets an active summary mode",
      cfg.get("active_summary_mode") == "general",
      f"active_summary_mode={cfg.get('active_summary_mode')!r}")

reslug = "_test_resummary"
res_meta = {
    "slug": reslug,
    "title": "Original Session",
    "started_at": "2026-01-02T09:00:00",
    "duration": 2.0,
    "status": "done",
    "summary": "Original summary",
    "transcript": "We agreed on owners and dates for the launch checklist.",
    "speakers": 2,
}
with open(os.path.join(muesli_module.REC_DIR, reslug + ".json"), "w", encoding="utf-8") as f:
    json.dump(res_meta, f)
make_silent_wav(os.path.join(TEST_SHARED_DIR, reslug + ".wav"), duration_s=2)
original_summarize = m.summarize
try:
    m.summarize = lambda transcript, prompt_text=None: {
        "title": "Launch Plan Review",
        "summary": "The team reviewed launch owners, deadlines, and follow-up actions.",
        "speakers": 2,
        "corrections": "",
        "bugs": "",
    }
    updated = m.resummarize_session(
        res_meta,
        prompt_text="Custom prompt {transcript}",
        mode_id="work_meeting",
        mode_title="Work Meeting",
    )
    check("resummarize_session returns done status", updated.get("status") == "done",
          f"status={updated.get('status')}, error={updated.get('error')}")
    check("resummarize_session updates slug from AI title",
          updated.get("slug") == "launch-plan-review",
          f"slug={updated.get('slug')!r}")
    check("resummarize_session stores summary mode metadata",
          updated.get("summary_mode") == "work_meeting" and updated.get("summary_mode_title") == "Work Meeting",
          f"summary_mode={updated.get('summary_mode')!r}, title={updated.get('summary_mode_title')!r}")
    check("resummarize_session renames shared audio",
          os.path.exists(os.path.join(TEST_SHARED_DIR, "launch-plan-review.wav")) and
          not os.path.exists(os.path.join(TEST_SHARED_DIR, reslug + ".wav")))
    check("resummarize_session writes updated transcript markdown",
          os.path.exists(os.path.join(TEST_SHARED_DIR, "launch-plan-review.txt")))
finally:
    m.summarize = original_summarize

# ── 6c. audio input device resolution ────────────────────────────────────────
print("\n[6b2] manual title/summary preservation")
manual_slug = "_test_manual_override"
manual_meta = {
    "slug": manual_slug,
    "title": "Manual Project Name",
    "title_manual": True,
    "started_at": "2026-01-03T11:00:00",
    "duration": 2.0,
    "status": "done",
    "summary": "Manual summary that should survive later AI work.",
    "summary_manual": True,
    "transcript": "The team reviewed launch blockers and assigned follow-up owners.",
    "speakers": 2,
}
with open(os.path.join(muesli_module.REC_DIR, manual_slug + ".json"), "w", encoding="utf-8") as f:
    json.dump(manual_meta, f)
make_silent_wav(os.path.join(TEST_SHARED_DIR, manual_slug + ".wav"), duration_s=2)
original_summarize = m.summarize
try:
    m.summarize = lambda transcript, prompt_text=None: {
        "title": "AI Replacement Title",
        "summary": "AI replacement summary that should not win.",
        "speakers": 4,
        "corrections": "",
        "bugs": "",
    }
    updated = m.resummarize_session(
        manual_meta,
        prompt_text="Custom prompt {transcript}",
        mode_id="general",
        mode_title="General",
    )
    check("manual title survives re-summarize",
          updated.get("title") == "Manual Project Name",
          f"title={updated.get('title')!r}")
    check("manual summary survives re-summarize",
          updated.get("summary") == "Manual summary that should survive later AI work.",
          f"summary={updated.get('summary')!r}")
    check("manual title still drives slug rename after re-summarize",
          updated.get("slug") == "manual-project-name",
          f"slug={updated.get('slug')!r}")
finally:
    m.summarize = original_summarize

print("\n[6c] audio input resolution")
saved_cfg = muesli_module._load_config()
saved_list_inputs = muesli_module._list_audio_input_devices
saved_backend = muesli_module._preferred_recording_backend
try:
    cfg_override = dict(saved_cfg)
    cfg_override["audio_input_backend"] = "pyaudio"
    cfg_override["audio_input_device"] = "7"
    with open(os.path.join(TEST_HOME, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg_override, f)
    muesli_module._list_audio_input_devices = lambda backend=None: [
        {"id": "", "label": "System Default", "backend": "pyaudio", "name": "System Default"},
        {"id": "7", "label": "USB Mic", "backend": "pyaudio", "name": "USB Mic"},
    ]
    muesli_module._preferred_recording_backend = lambda: "pyaudio"
    check("audio input resolver returns configured device id when available",
          muesli_module._resolve_audio_input_device("pyaudio") == 7)
    check("audio input resolver ignores device settings for another backend",
          muesli_module._resolve_audio_input_device("sounddevice") is None)
finally:
    muesli_module._list_audio_input_devices = saved_list_inputs
    muesli_module._preferred_recording_backend = saved_backend
    with open(os.path.join(TEST_HOME, "config.json"), "w", encoding="utf-8") as f:
        json.dump(saved_cfg, f)

# ── 6d. OpenAI transcription backend routing ────────────────────────────────
print("\n[6d] OpenAI transcription backend")
saved_cfg = muesli_module._load_config()
saved_openai_transcribe = muesli_module._transcribe_openai
try:
    cfg_override = dict(saved_cfg)
    cfg_override["transcription_backend"] = "openai_realtime"
    cfg_override["openai_transcription_model"] = "gpt-4o-transcribe"
    cfg_override["openai_api_key"] = "test-openai-key"
    with open(os.path.join(TEST_HOME, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg_override, f)
    openai_calls = []
    progress_events = []
    muesli_module._transcribe_openai = lambda audio_path: (openai_calls.append(audio_path) or "Cloud transcript from OpenAI.")
    transcript = m.transcribe_with_progress(
        "existing-recording.mp3",
        on_percent=lambda pct: progress_events.append(pct),
    )
    check("transcribe_with_progress routes to OpenAI helper for realtime-configured file transcription",
          transcript == "Cloud transcript from OpenAI." and openai_calls == ["existing-recording.mp3"],
          f"transcript={transcript!r}, calls={openai_calls!r}")
    check("OpenAI transcription path emits bounded progress callbacks",
          progress_events == [5, 100],
          f"progress={progress_events!r}")
finally:
    muesli_module._transcribe_openai = saved_openai_transcribe
    with open(os.path.join(TEST_HOME, "config.json"), "w", encoding="utf-8") as f:
        json.dump(saved_cfg, f)

# ── 7. process_file (silent wav) ─────────────────────────────────────────────
print("\n[6e] GUI manual override finalize path")
saved_gui_ffmpeg_available = muesli_gui_module._ffmpeg_available
saved_gui_transcription_backend = muesli_gui_module._transcription_backend
saved_gui_transcribe_segments = muesli_gui_module._transcribe_segments
saved_gui_wait = muesli_gui_module._wait_for_processing_resume
saved_gui_llm_generate = muesli_gui_module._llm_generate
gui_slug = "_test_gui_manual_override"
gui_meta = {
    "slug": gui_slug,
    "title": "Chosen Manual Title",
    "title_manual": True,
    "started_at": "2026-01-04T08:30:00",
    "duration": 2.0,
    "status": "processing",
    "summary": "Chosen manual summary",
    "summary_manual": True,
    "transcript": "",
    "speakers": 0,
}
with open(os.path.join(muesli_gui_module.REC_DIR, gui_slug + ".json"), "w", encoding="utf-8") as f:
    json.dump(gui_meta, f)
make_silent_wav(os.path.join(muesli_gui_module.REC_DIR, gui_slug + ".wav"), duration_s=2)
gui_progress = []
try:
    muesli_gui_module._ffmpeg_available = lambda: False
    muesli_gui_module._transcription_backend = lambda: "local"
    muesli_gui_module._wait_for_processing_resume = lambda: None
    muesli_gui_module._transcribe_segments = lambda audio_file, beam_size=5: (
        [types.SimpleNamespace(text="Automatic transcript", end=2.0)],
        types.SimpleNamespace(duration=2.0),
    )
    muesli_gui_module._llm_generate = lambda prompt_text, ollama_timeout=None: json.dumps({
        "title": "AI Wrong Title",
        "summary": "AI wrong summary",
        "speakers": 3,
        "corrections": "",
        "bugs": "",
    })
    muesli_gui_module.process_recording(
        gui_meta,
        lambda updated, stage: gui_progress.append((updated.get("slug"), stage)),
    )
    check("GUI process_recording finishes successfully", gui_meta.get("status") == "done",
          f"status={gui_meta.get('status')}, error={gui_meta.get('error')}")
    check("GUI process_recording preserves manual title",
          gui_meta.get("title") == "Chosen Manual Title",
          f"title={gui_meta.get('title')!r}")
    check("GUI process_recording preserves manual summary",
          gui_meta.get("summary") == "Chosen manual summary",
          f"summary={gui_meta.get('summary')!r}")
    check("GUI process_recording renames slug from manual title",
          gui_meta.get("slug") == "chosen-manual-title",
          f"slug={gui_meta.get('slug')!r}")
    gui_txt_path = os.path.join(muesli_gui_module.SHARED_DIR, "chosen-manual-title.txt")
    check("GUI process_recording rewrites the text sidecar with manual edits",
          os.path.exists(gui_txt_path) and
          "Chosen Manual Title" in open(gui_txt_path, "r", encoding="utf-8").read() and
          "Chosen manual summary" in open(gui_txt_path, "r", encoding="utf-8").read(),
          f"txt_path={gui_txt_path!r}, progress={gui_progress!r}")
finally:
    muesli_gui_module._ffmpeg_available = saved_gui_ffmpeg_available
    muesli_gui_module._transcription_backend = saved_gui_transcription_backend
    muesli_gui_module._transcribe_segments = saved_gui_transcribe_segments
    muesli_gui_module._wait_for_processing_resume = saved_gui_wait
    muesli_gui_module._llm_generate = saved_gui_llm_generate

print("\n[7] process_file")
with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
    tmp_wav = f.name
try:
    make_silent_wav(tmp_wav, duration_s=2)
    session = m.process_file(tmp_wav)
    check("process_file returns dict", isinstance(session, dict))
    check("process_file has status=done", session.get("status") == "done",
          f"status={session.get('status')}, error={session.get('error')}")
    check("process_file has audio_path", "audio_path" in session)
    check("process_file audio exists",
          session.get("audio_path") and os.path.exists(session["audio_path"]))
    check("process_file has transcript", "transcript" in session)
    check("GUI reprocess helper allows saved sessions with audio",
          muesli_gui_module._can_reprocess_meta(session))
    check("GUI reprocess helper rejects sessions without saved audio",
          not muesli_gui_module._can_reprocess_meta({"slug": "no-such-slug-999"}))
    print(f"       slug: {session.get('slug')}")
    print(f"       title: {session.get('title')}")
finally:
    os.unlink(tmp_wav) if os.path.exists(tmp_wav) else None

print("\n[7b] process_file m4a import")
with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
    tmp_wav = f.name
tmp_m4a = tempfile.NamedTemporaryFile(suffix=".m4a", delete=False).name
try:
    make_silent_wav(tmp_wav, duration_s=2)
    if muesli_module._ffmpeg_available():
        subprocess.run(
            ["ffmpeg", "-y", "-i", tmp_wav, tmp_m4a],
            capture_output=True,
            check=True,
        )
        session = m.process_file(tmp_m4a)
        check("process_file handles m4a imports", isinstance(session, dict) and session.get("status") == "done",
              f"status={session.get('status')}, error={session.get('error')}")
        check("m4a import resolves to canonical playable output",
              str(session.get("audio_path", "")).lower().endswith((".mp3", ".wav")),
              f"audio_path={session.get('audio_path')!r}")
        check("m4a import does not leave orphaned m4a sidecars in shared dir",
              not any(name.lower().endswith(".m4a") for name in os.listdir(TEST_SHARED_DIR)))
    else:
        print("  SKIP  ffmpeg unavailable for m4a import coverage")
finally:
    for path in (tmp_wav, tmp_m4a):
        if os.path.exists(path):
            os.unlink(path)

# ── 8. _resume_session (test with a tiny fake interrupted session) ────────────
print("\n[8] resume_session")
# Create a fake interrupted session with a tiny audio file
_test_slug = "_test_resume_check"
_test_wav = os.path.join(muesli_module.REC_DIR, _test_slug + ".wav")
_test_json = os.path.join(muesli_module.REC_DIR, _test_slug + ".json")
make_silent_wav(_test_wav, duration_s=2)
import json as _json
_meta = {
    "slug": _test_slug,
    "title": _test_slug,
    "started_at": "2026-01-01T00:00:00",
    "duration": 2.0,
    "status": "error",
    "error": "Interrupted — app closed during processing",
    "summary": "", "transcript": "", "speakers": 0,
}
m.transcribe_with_progress = lambda audio_path, on_percent=None: "Project feedback 😀 includes unicode notes and follow-up."
with open(_test_json, "w", encoding="utf-8") as _f:
    _json.dump(_meta, _f)
# Resume just this one session directly (skip resume_interrupted which would hit all)
progress_log = []
result = m._resume_session(_meta, _test_wav,
    on_progress=lambda slug, stage: progress_log.append((slug, stage)))
check("_resume_session returns dict", isinstance(result, dict))
check("_resume_session status=done", result.get("status") == "done",
      f"status={result.get('status')}, error={result.get('error')}")
check("_resume_session has transcript", "transcript" in result)
check("_resume_session preserves unicode transcript text", "😀" in result.get("transcript", ""))
check("_resume_session progress callbacks fired", len(progress_log) >= 2,
      f"got {progress_log}")
_txt_slug = result.get("slug")
_txt_path = os.path.join(TEST_SHARED_DIR, _txt_slug + ".txt") if _txt_slug else ""
check("_resume_session writes utf-8 transcript sidecar",
      bool(_txt_path and os.path.exists(_txt_path) and "😀" in open(_txt_path, "r", encoding="utf-8").read()),
      f"txt_path={_txt_path!r}")
print(f"       slug: {result.get('slug')}, events: {progress_log}")
# Clean up
for _p in [_test_json, _test_wav]:
    if os.path.exists(_p):
        os.remove(_p)
for _d in [muesli_module.REC_DIR, m._shared_dir]:
    for _f in os.listdir(_d):
        if "_test" in _f or _f.startswith("recording") and _f.endswith((".json", ".wav", ".mp3", ".txt")):
            try: os.remove(os.path.join(_d, _f))
            except: pass

# ── 9. sounddevice live chunk emission regression ─────────────────────────────
print("\n[9] sounddevice live chunks")
chunk_slug = "_test_sd_chunk"
collector = SubmitCollector()
m2 = Muesli()
m2._recording = True
m2._record_backend = "sounddevice"
m2._rec_slug = chunk_slug
m2._frames = []
m2._chunk_idx = 0
m2._sample_width = 2
m2._sd_chunk_frames = []
m2._pipeline = collector
payload = struct.pack(f"<{muesli_module.CHUNK}h", *([0] * muesli_module.CHUNK))
for _ in range(muesli_module._READS_PER_CHUNK):
    m2._record_sounddevice(FakeInputBlock(payload), muesli_module.CHUNK, None, None)
check("live chunk size is 10 seconds", muesli_module.LIVE_CHUNK_SECONDS == 10,
      f"got {muesli_module.LIVE_CHUNK_SECONDS}")
check("sounddevice emits chunk before stop", len(collector.paths) == 1,
      f"emitted {len(collector.paths)} chunks")
chunk_path = collector.paths[0] if collector.paths else None
check("emitted chunk file exists", bool(chunk_path and os.path.exists(chunk_path)))
if chunk_path and os.path.exists(chunk_path):
    wf = wave.open(chunk_path, "rb")
    try:
        duration = wf.getnframes() / wf.getframerate()
    finally:
        wf.close()
    check("emitted chunk duration is about 10 seconds",
          abs(duration - muesli_module.LIVE_CHUNK_SECONDS) < 0.5,
          f"duration={duration}")
    os.remove(chunk_path)

# ── 10. rename wiring regression ──────────────────────────────────────────────
print("\n[10] rename wiring")
gui_source = open(os.path.join(os.path.dirname(__file__), "muesli_gui.py"), "r", encoding="utf-8").read()
check("sessions list rename is double-click only",
      '<Double-Button-1>", self._rename_selected_from_list' in gui_source and
      '<ButtonRelease-1>", self._rename_selected_on_click' not in gui_source)
check("detail title rename remains single-click",
      'self._title_lbl.bind("<Button-1>", self._on_rename_click)' in gui_source)
check("detail title rename stays available during live recording",
      'if self._on_rename and (self._current or self._live_mode):' in gui_source)
check("detail panel exposes an editable summary action",
      'self._edit_summary_btn = RoundedButton(' in gui_source and
      'command=self._on_edit_summary_click' in gui_source and
      'def _on_edit_summary_click(self):' in gui_source)
check("manual title and summary flags are persisted from GUI edits",
      'meta[\"title_manual\"] = True' in gui_source and
      'self._cur_meta[\"summary_manual\"] = True' in gui_source and
      'self._live_manual_summary_active = True' in gui_source)
check("detail pane includes an explicit editability hint",
      'Click the title to rename. Use Edit to change the summary. Manual edits are preserved.' in gui_source)

# ── 11. live status + summary pipeline regression ────────────────────────────
print("\n[11] live status + summary pipeline")
check("detail live transcript accepts status_note",
      'def set_live_transcript(self, text, processing=True, status_note="")' in gui_source)
check("GUI live chunk pipeline no longer blocks on _llm_generate",
      "Keep live transcript delivery independent from slower LLM work." in gui_source)
api_source = open(os.path.join(os.path.dirname(__file__), "muesli.py"), "r", encoding="utf-8").read()
check("API live chunk pipeline no longer blocks on _llm_generate",
      "Keep live transcript delivery independent from slower LLM work." in api_source)
check("GUI Ollama timeout increased for final summary",
      'timeout=ollama_timeout or 420' in gui_source)
check("API Ollama timeout increased for final summary",
      'timeout=420' in api_source)

# ── 12. provisional summary + model preference regression ────────────────────
print("\n[12] provisional summary + model preference")
preferred = muesli_module._recommend_ollama_model([
    "qwen3.5:4b", "qwen3:8b", "deepseek-r1:8b", "qwen3.5:9b"
])
check("preferred Ollama model favors lower-latency summary model",
      preferred == "deepseek-r1:8b", f"got {preferred!r}")
check("GUI starts provisional summary on first spoken chunk",
      'if text and not self._provisional_summary_started:' in gui_source)
check("GUI provisional summary uses bounded timeout",
      '_generate_ai_fields(transcript_snapshot, ollama_timeout=60)' in gui_source)
provisional_section = gui_source.split("def _apply_provisional_summary", 1)[1].split("def _tick_timer", 1)[0]
check("GUI keeps provisional preview out of saved metadata before final completion",
      'meta["summary"] = self._live_preview.get("summary")' not in gui_source and
      "_save_meta(" not in provisional_section)
check("GUI clears provisional AI title and summary once recording stops",
      'summary="Final summary and rename will appear when processing completes."' in gui_source and
      'title=self._live_preview.get("title")' not in gui_source)
check("GUI falls back to a transcript excerpt when provisional summary is blank",
      'summary = str(ai.get("summary", "")).strip() or self._fallback_live_summary(transcript_snapshot)' in gui_source)
check("GUI tracks queued live chunks for ETA messaging",
      'def progress_snapshot(self):' in gui_source and 'rough catch-up ETA' in gui_source)
check("GUI scopes live chunk callbacks to the active recording generation",
      'self._live_generation += 1' in gui_source and
      'def _on_chunk_transcribed(self, generation, n, text):' in gui_source and
      'if generation != self._live_generation:' in gui_source)
hotkey_source = open(os.path.join(os.path.dirname(__file__), "muesli_hotkey.py"), "r", encoding="utf-8").read()
check("GUI hands normal relaunches to existing window instead of forcing record",
      'requested_cmd = "record" if "--record" in sys.argv else "show"' in gui_source)
check("GUI uses a Windows single-instance lock before launching",
      "_acquire_single_instance()" in gui_source and "_release_single_instance()" in gui_source)
check("GUI applies Windows AppUserModelID before Tk root creation",
      "_apply_windows_app_identity()" in gui_source and
      gui_source.index("_apply_windows_app_identity()") < gui_source.index("app = MuesliApp(launch_token=launch_token, launch_probe=launch_probe)"))
check("GUI desktop shortcut uses hidden launcher script instead of pythonw",
      'GUI_LAUNCHER = os.path.join(APP_DIR, "muesli_gui_launcher.vbs")' in gui_source and
      'args = f\'"{GUI_LAUNCHER}"\'' in gui_source and
      'return launcher, args' in gui_source)
check("GUI hotkey shortcut uses hidden launcher script instead of pythonw",
      'HOTKEY_AGENT_LAUNCHER = os.path.join(APP_DIR, "muesli_hotkey_launcher.vbs")' in gui_source and
      'return launcher, f\'"{HOTKEY_AGENT_LAUNCHER}"\'' in gui_source)
check("GUI supports quit IPC from the tray sidecar",
      'elif data == "quit":' in gui_source and 'self.after(0, self._on_close)' in gui_source)
check("GUI starts runtime-state polling after launch",
      'self.after(250, self._poll_runtime_state)' in gui_source)
check("GUI restarts stale Windows sidecar when runtime state has no live sidecar pid",
      'self.after(750, ensure_windows_hotkey_agent_current)' in gui_source and 'if _pid_alive(state.get("sidecar_pid")):' in gui_source)
check("Hotkey sidecar debounces repeated WM_HOTKEY events",
      "DEBOUNCE_SECONDS = 1.0" in hotkey_source and "time.monotonic()" in hotkey_source)
check("Hotkey sidecar prefers IPC before spawning a new GUI process",
      'launch_app(record=True)' in hotkey_source and 'if send_to_existing(cmd):' in hotkey_source)
check("Hotkey sidecar launches GUI through the same wrapper-based app command",
      'WSCRIPT_EXE = os.path.join' in hotkey_source and
      'GUI_LAUNCHER = os.path.join(APP_DIR, "muesli_gui_launcher.vbs")' in hotkey_source and
      'cmd = [WSCRIPT_EXE, GUI_LAUNCHER]' in hotkey_source)
check("Hotkey tray decodes NOTIFYICON_VERSION_4 mouse events from LOWORD(lParam)",
      'def _tray_event_code(lparam):' in hotkey_source and
      'event_code = _tray_event_code(lparam)' in hotkey_source and
      'if event_code in (WM_RBUTTONUP, WM_LBUTTONUP):' in hotkey_source)
check("Hotkey tray explicitly requests normal tooltip display",
      'NIF_SHOWTIP = 0x0080' in hotkey_source and
      'NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP' in hotkey_source)
check("Hotkey sidecar exposes tray pause/resume and exit actions",
      '"Pause Processing"' in hotkey_source and '"Resume Processing"' in hotkey_source and '"Exit"' in hotkey_source)
check("Hotkey sidecar can request graceful GUI quit",
      'send_to_existing("quit")' in hotkey_source)
check("Hotkey tray owner-draws menu items for explicit readable colors",
      'WM_MEASUREITEM = 0x002C' in hotkey_source and
      'WM_DRAWITEM = 0x002B' in hotkey_source and
      'MF_OWNERDRAW = 0x0100' in hotkey_source and
      'def _draw_menu_item(self, lparam):' in hotkey_source and
      'COLOR_MENUTEXT' in hotkey_source and
      'DrawTextW' in hotkey_source)
shortcut_script = open(os.path.join(os.path.dirname(__file__), "refresh_windows_shortcuts.ps1"), "r", encoding="utf-8").read()
check("shortcut refresh stamps AppUserModelID and relaunch icon metadata",
      'SetAppDetails' in shortcut_script and 'PROPERTYKEY(AppUserModelGuid, 5)' in shortcut_script and 'PROPERTYKEY(AppUserModelGuid, 3)' in shortcut_script)
check("shortcut refresh launches sidecar with hidden python.exe and startup wscript wrapper",
      'Start-Process -FilePath $python' in shortcut_script and
      'TargetPath $wscript' in shortcut_script and
      'muesli_hotkey_launcher.vbs' in shortcut_script)
check("shortcut refresh writes desktop and record launchers through the GUI wrapper",
      'muesli_gui_launcher.vbs' in shortcut_script and
      'Write-Shortcut -Path $desktop -TargetPath $wscript' in shortcut_script and
      'Write-Shortcut -Path $recordShortcut -TargetPath $wscript' in shortcut_script)
script_run = subprocess.run(
    [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        os.path.join(os.path.dirname(__file__), "refresh_windows_shortcuts.ps1"),
        "-DryRun",
        "-SkipHotkeyRestart",
    ],
    capture_output=True,
    text=True,
)
check("shortcut refresh script parses and runs in DryRun mode",
      script_run.returncode == 0,
      detail=(script_run.stderr or script_run.stdout or "").strip()[:240])

print("\n[13] summary mode UI wiring")
check("GUI exposes a summary mode bar",
      'text="Summary Mode"' in gui_source and
      'self._summary_modes_frame = tk.Frame(mode_bar' in gui_source and
      'mode_bar = tk.Frame(self, bg=DARK_BG, padx=18)' in gui_source and
      'mode_bar.pack(fill="x", pady=(0, 10))' in gui_source)
check("Ctrl+P opens the summary modes dialog",
      'self.bind_all("<Control-p>", lambda e: (self._open_summary_modes_dialog(), "break")[1])' in gui_source)
check("summary mode button reprocesses the selected session and sets the default",
      'if self._cur_meta and not self._recording and self._cur_meta.get("status") != "processing":' in gui_source and
      'self._resummarize_session(self._cur_meta, mode)' in gui_source)
check("General mode is protected in the edit dialog",
      'self._delete_btn.set_enabled(not is_general)' in gui_source and
      'self._title_entry.configure(state="disabled" if is_general else "normal")' in gui_source)
check("API exposes session re-summarization support",
      'def resummarize_session(self, session_or_slug, prompt_text=None, mode_id=None, mode_title=None):' in api_source and
      '"summary_mode": mode_id or mode["id"]' in api_source)
check("Sessions with saved audio expose a reprocess action in the detail panel",
      'text="Reprocess"' in gui_source and
      'self._resubmit_btn.set_enabled(_can_reprocess_meta(meta))' in gui_source and
      'def _resubmit_current(self):' in gui_source)
check("GUI reprocess path forces a fresh transcript pass from saved audio",
      'target["transcript"] = ""' in gui_source and
      'target["summary"] = "Reprocessing from saved audio..."' in gui_source)
check("GUI reprocess/resume progress is persisted into processing_stage metadata",
      'target["processing_stage"] = "queued"' in gui_source and
      'meta["processing_stage"] = stage_text' in gui_source and
      'meta.pop("processing_stage", None)' in gui_source)
check("Detail pane shows live processing-stage text for saved-audio reprocessing",
      'status_text = f"Processing: {processing_stage}"' in gui_source and
      'status_note=processing_stage or "Processing saved audio..."' in gui_source)
check("Detail pane uses a split review surface with a recording-flow card",
      'text="Recording flow"' in gui_source and
      'self._flow_note_var = tk.StringVar' in gui_source and
      'flow_card = tk.Frame(split, bg=ITEM_BG' in gui_source)
check("Detail pane exposes tabbed summary and transcript views",
      'self._summary_tab_btn = RoundedButton(' in gui_source and
      'self._transcript_tab_btn = RoundedButton(' in gui_source and
      'command=lambda: self._set_content_view("summary")' in gui_source and
      'command=lambda: self._set_content_view("transcript")' in gui_source)
check("Detail pane refreshes active tab state and swaps view-specific actions",
      'def _refresh_content_view(self):' in gui_source and
      'self._open_transcript_btn.pack(side="left")' in gui_source and
      'self._edit_summary_btn.pack(side="left")' in gui_source)
check("GUI publishes runtime state immediately and keeps polling alive after transient errors",
      'self._publish_runtime_state()' in gui_source and
      'runtime_poll_failed' in gui_source and
      'finally:\n            self.after(1000, self._poll_runtime_state)' in gui_source)
check("Pause-processing refresh targets the actual RoundedButton instead of a generic child widget",
      'self._pause_btn = RoundedButton(' in gui_source and
      'self._pause_btn.pack(side="right", padx=(0, 10))' in gui_source and
      'top.winfo_children()[0]' not in gui_source)
check("Transcript sidecars are always written as UTF-8 during finalize paths",
      'with open(txt_path, "w", encoding="utf-8") as f:' in api_source and
      'def _write_session_text_file(slug, title, summary, transcript, corrections="", bugs=""):' in gui_source and
      'with open(_txt_path(slug), "w", encoding="utf-8") as f:' in gui_source)

print("\n[14] launch splash wiring")
launcher_source = open(os.path.join(os.path.dirname(__file__), "muesli_gui_launcher.vbs"), "r", encoding="utf-8").read()
splash_source = open(os.path.join(os.path.dirname(__file__), "muesli_splash.ps1"), "r", encoding="utf-8").read()
bootstrap_source = open(os.path.join(os.path.dirname(__file__), "muesli_gui_bootstrap.py"), "r", encoding="utf-8").read()
check("GUI launcher creates a launch token and status file before starting Python",
      'token = CStr(Fix(Timer * 1000))' in launcher_source and
      'statusPath = root & "\\.launch_status_" & token & ".json"' in launcher_source and
      'CreateTextFile(statusPath, True)' in launcher_source and
      '"detail"' in launcher_source)
check("GUI launcher starts the PowerShell splash before Python",
      'muesli_splash.ps1' in launcher_source and
      'shell.Run splashCmd, 0, False' in launcher_source and
      '--launch-token ' in launcher_source)
check("GUI launcher uses the bootstrap entrypoint and explicit PowerShell path for startup tracing",
      'bootstrapScript = root & "\\muesli_gui_bootstrap.py"' in launcher_source and
      'powershellExe = shell.ExpandEnvironmentStrings("%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe")' in launcher_source and
      'tracePath = root & "\\.launch_trace_" & token & ".log"' in launcher_source and
      'AppendTrace("python_launch_requested"' in launcher_source)
check("GUI launcher prefers pythonw for the normal hidden app launch path",
      # The exact path string changed when the venv resolution was rewritten
      # (now derived from venvRoot which prefers %LOCALAPPDATA%\muesli\.venv
      # over the in-source .venv\), but the pythonw-preference logic itself
      # is unchanged: define pythonwExe, default to pythonExe, then upgrade
      # to pythonwExe only if it exists.
      'pythonwExe = venvRoot & "\\Scripts\\pythonw.exe"' in launcher_source and
      'selectedPython = pythonExe' in launcher_source and
      'If fso.FileExists(pythonwExe) Then' in launcher_source and
      'selectedPython = pythonwExe' in launcher_source)
check("Python bootstrap updates launch status before importing the full GUI module",
      'MUESLI_BOOTSTRAP_STARTED_AT' in bootstrap_source and
      'progress=8' in bootstrap_source and
      'runpy.run_path(gui_path, run_name="__main__")' in bootstrap_source)
check("GUI startup publishes splash progress and closes it when ready",
      'LAUNCH_STATUS_PREFIX = ".launch_status_"' in gui_source and
      'LAUNCH_TRACE_PREFIX = ".launch_trace_"' in gui_source and
      'def _update_launch_status(token, stage, progress=None, close=False, detail=""):' in gui_source and
      'def _finish_launch_status(token):' in gui_source and
      'self.after(120, self._mark_launch_ready)' in gui_source)
check("GUI startup records timed launch milestones with slow-step thresholds",
      'class _LaunchProbe:' in gui_source and
      'threshold_ms=2500' in gui_source and
      'python_import_bootstrap_complete' in gui_source)
check("GUI handoff path closes splash when routing to an existing instance",
      '"Opening existing Muesli window..."' in gui_source and
      '_finish_launch_status(launch_token)' in gui_source)
check("PowerShell splash renders the logo, title, and timer-driven progress bar",
      'PictureBox' in splash_source and
      'Muesli' in splash_source and
      '[System.Windows.Forms.Application]::EnableVisualStyles()' in splash_source and
      '$progress.Style = "Continuous"' in splash_source and
      '$detail = New-Object System.Windows.Forms.Label' in splash_source and
      '$progress.Value = $currentProgress' in splash_source and
      '$hint.Text = "$currentProgress% complete"' in splash_source)
check("PowerShell splash progress only changes from reported milestones, not elapsed time",
      '$displayProgress' not in splash_source and '$targetProgress' not in splash_source and
      '$progress.Value = $currentProgress' in splash_source)
check("PowerShell splash shows actual step notes from launch status detail",
      '$payload.detail' in splash_source and '$detail.Text = [string]$payload.detail' in splash_source)
check("PowerShell splash contributes launch trace events for diagnosing pre-GUI delays",
      '$tracePath = Join-Path $Root (".launch_trace_" + $Token + ".log")' in splash_source and
      'Add-LaunchTrace -Event "splash_process_started"' in splash_source and
      'Add-LaunchTrace -Event "splash_window_shown"' in splash_source)
check("Bootstrap logs import/startup exceptions instead of leaving the splash looking hung",
      'python_bootstrap_failed' in bootstrap_source and
      'python_bootstrap_traceback' in bootstrap_source and
      '"Startup failed"' in bootstrap_source)

print("\n[15] audio input settings wiring")
check("GUI settings exposes recording input selection",
      '"Recording Input"' in gui_source and 'audio_input = tk.StringVar' in gui_source)
check("GUI settings exposes OpenAI cloud transcription controls",
      '"Transcription"' in gui_source and
      'transcription_backend = tk.StringVar' in gui_source and
      '"OpenAI Cloud": "openai"' in gui_source and
      '"OpenAI Realtime": "openai_realtime"' in gui_source and
      'openai_transcription_model = tk.StringVar' in gui_source and
      'openai_api_key = tk.StringVar' in gui_source)
check("Runtime visibility includes effective transcription backend/model",
      'runtime_transcription_backend_var = tk.StringVar()' in gui_source and
      'runtime_transcription_model_var = tk.StringVar()' in gui_source and
      '"Effective transcription backend"' in gui_source)
check("GUI recorder applies configured input device with fallback to default",
      'selected_device = _resolve_audio_input_device(self._backend)' in gui_source and
      'open_kwargs["input_device_index"] = selected_device' in gui_source and
      'stream_kwargs["device"] = selected_device' in gui_source)
check("API recorder applies configured input device with fallback to default",
      'selected_device = _resolve_audio_input_device(self._record_backend)' in api_source and
      'open_kwargs["input_device_index"] = selected_device' in api_source and
      'stream_kwargs["device"] = selected_device' in api_source)
check("Settings show runtime visibility for summary backend/model and recording input",
      '"Runtime Visibility"' in gui_source and
      'runtime_backend_var = tk.StringVar()' in gui_source and
      'runtime_audio_var = tk.StringVar()' in gui_source and
      'ollama.exe runner' in gui_source)
check("API exposes an OpenAI transcription helper and backend selector",
      'def _transcribe_openai(audio_file_path):' in api_source and
      'def _transcription_backend(cfg=None):' in api_source and
      '"openai_realtime"' in api_source and
      'https://api.openai.com/v1/audio/transcriptions' in api_source)
check("GUI final processing re-transcribes the full file with OpenAI when selected",
      'transcription_backend = _transcription_backend()' in gui_source and
      'if transcript is None or transcription_backend == "openai":' in gui_source and
      'transcript = _transcribe_openai(audio_file)' in gui_source)
check("GUI includes an OpenAI Realtime live transcription path",
      'class OpenAIRealtimeTranscriber:' in gui_source and
      'wss://api.openai.com/v1/realtime?intent=transcription' in gui_source and
      '"type": "input_audio_buffer.append"' in gui_source and
      '"conversation.item.input_audio_transcription.delta"' in gui_source and
      'self._realtime_transcriber.append_audio' in gui_source)
check("GUI no longer hard-depends on audioop at import time",
      'try:\n    import audioop\nexcept ImportError:\n    audioop = None' in gui_source and
      'def _resample_pcm16_mono(pcm_bytes, src_rate, dst_rate):' in gui_source and
      'if audioop is not None:' in gui_source)
check("Recorder can feed realtime streaming callbacks frame-by-frame",
      'def start(self, on_chunk=None, on_frame=None):' in gui_source and
      'self._on_frame  = on_frame' in gui_source and
      'if self._on_frame is not None:' in gui_source)
check("Settings expose Obsidian vault configuration and export folder",
      '"Obsidian Export"' in gui_source and
      'obsidian_vault_dir = tk.StringVar' in gui_source and
      'obsidian_export_folder = tk.StringVar' in gui_source)
check("GUI can export the selected session to Obsidian",
      'def _export_session_to_obsidian(meta, cfg=None):' in gui_source and
      'text="Export Obsidian"' in gui_source and
      'def _export_current_to_obsidian(self):' in gui_source)
check("Detail panel includes the recording-flow diagram surface",
      'text="Recording flow"' in gui_source and
      'self._process_canvas = tk.Canvas' in gui_source and
      'def _draw_process_diagram(self):' in gui_source and
      'def _sync_process_diagram(self, meta):' in gui_source)

# ── Summary ──────────────────────────────────────────────────────────────────
print("\n[16] local service surface")
service_source = open(os.path.join(os.path.dirname(__file__), "muesli_service.py"), "r", encoding="utf-8").read()
check("service exposes health, capabilities, and OpenAPI discovery",
      'def _openapi_spec(port):' in service_source and
      '"/health"' in service_source and
      '"/capabilities"' in service_source and
      '["openapi.json"]' in service_source)
check("service exposes deterministic note routes and async job routes",
      '"/notes/search"' in service_source and
      '"/jobs/transcribe-file"' in service_source and
      '"/jobs/ingest-voice-note"' in service_source and
      '"/jobs/reprocess-note"' in service_source)
check("API exposes search/latest/overwrite/delete helpers for the service layer",
      'def get_latest_session(self):' in api_source and
      'def search_sessions(self, query, component="any", limit=20):' in api_source and
      'def overwrite_session(self, slug, **updates):' in api_source and
      'def delete_session(self, slug):' in api_source)
check("API sidecar writing is centralized so service overwrites stay in sync",
      'def _write_note_sidecar(shared_dir, slug, title, summary, corrections, bugs, transcript):' in api_source and
      '_write_note_sidecar(' in api_source)


class FakeServiceApp:
    def __init__(self):
        self._recording = False
        self._rec_slug = None
        self._rec_started = None
        self.notes = [{
            "slug": "alpha-note",
            "title": "Alpha Note",
            "summary": "Short summary",
            "transcript": "Alpha transcript about deadlines and owners.",
            "started_at": "2026-04-27T09:00:00",
            "duration": 12.5,
            "speakers": 2,
            "status": "done",
            "audio_path": os.path.join(TEST_SHARED_DIR, "alpha-note.wav"),
        }]
        make_silent_wav(self.notes[0]["audio_path"], duration_s=1)

    def list_sessions(self):
        return [dict(note) for note in self.notes]

    def get_latest_session(self):
        return dict(self.notes[0]) if self.notes else None

    def get_session(self, slug):
        for note in self.notes:
            if note["slug"] == slug:
                return dict(note)
        return None

    def delete_session(self, slug):
        for idx, note in enumerate(self.notes):
            if note["slug"] == slug:
                self.notes.pop(idx)
                return True
        return False

    def search_sessions(self, query, component="any", limit=20):
        query = str(query or "").lower()
        hits = []
        for note in self.notes:
            haystack = " ".join(str(note.get(field, "")) for field in ("title", "summary", "transcript", "started_at", "status")).lower()
            if query in haystack:
                match = dict(note)
                match["match_component"] = component
                match["match_excerpt"] = note["transcript"][:60]
                hits.append(match)
        return hits[:limit]

    def overwrite_session(self, slug, **updates):
        for note in self.notes:
            if note["slug"] == slug:
                note.update(updates)
                return dict(note)
        raise FileNotFoundError(slug)

    def transcribe(self, path):
        return f"transcript for {os.path.basename(path)}"

    def process_file(self, path):
        return {
            "slug": "imported-note",
            "title": "Imported Note",
            "summary": "Imported summary",
            "transcript": f"processed {os.path.basename(path)}",
            "started_at": "2026-04-27T10:00:00",
            "duration": 3.0,
            "speakers": 1,
            "status": "done",
            "audio_path": path,
        }

    def resummarize_session(self, session_or_slug, prompt_text=None, mode_id=None, mode_title=None):
        slug = session_or_slug["slug"] if isinstance(session_or_slug, dict) else str(session_or_slug)
        return {
            "slug": slug,
            "title": "Reprocessed Note",
            "summary": "Reprocessed summary",
            "transcript": "Fresh transcript",
            "started_at": "2026-04-27T11:00:00",
            "duration": 4.0,
            "speakers": 1,
            "status": "done",
            "audio_path": os.path.join(TEST_SHARED_DIR, slug + ".wav"),
            "summary_mode": mode_id or "general",
            "summary_mode_title": mode_title or "General",
        }

    def start_recording(self):
        self._recording = True
        self._rec_slug = "live-note"
        self._rec_started = types.SimpleNamespace(isoformat=lambda: "2026-04-27T12:00:00")

    def stop_recording(self):
        self._recording = False
        return {
            "slug": "live-note",
            "title": "Live Note",
            "summary": "Stopped cleanly",
            "transcript": "Captured transcript",
            "started_at": "2026-04-27T12:00:00",
            "duration": 5.0,
            "speakers": 1,
            "status": "done",
            "audio_path": os.path.join(TEST_SHARED_DIR, "live-note.wav"),
        }


def _http_json(url, method="GET", payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as res:
        return res.status, json.loads(res.read().decode("utf-8"))


fake_service_app = FakeServiceApp()
service = muesli_service_module.MuesliHttpService(app=fake_service_app)
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
service_port = sock.getsockname()[1]
sock.close()
http_server = muesli_service_module.serve(port=service_port, host="127.0.0.1", service=service)
http_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
http_thread.start()
try:
    status_code, health = _http_json(f"http://127.0.0.1:{service_port}/health")
    check("service health endpoint responds", status_code == 200 and health.get("ok") is True, detail=str(health))
    status_code, capabilities = _http_json(f"http://127.0.0.1:{service_port}/capabilities")
    check("service capabilities include components and jobs",
          status_code == 200 and "components" in capabilities and "supports_jobs" in capabilities,
          detail=str(capabilities))
    status_code, openapi = _http_json(f"http://127.0.0.1:{service_port}/openapi.json")
    check("service publishes OpenAPI for registry auto-discovery",
          status_code == 200 and "/jobs/transcribe-file" in openapi.get("paths", {}),
          detail=str(openapi.get("paths", {}).keys()))
    status_code, notes_payload = _http_json(f"http://127.0.0.1:{service_port}/notes")
    check("service lists notes over HTTP",
          status_code == 200 and len(notes_payload.get("notes", [])) == 1,
          detail=str(notes_payload))
    status_code, note_payload = _http_json(f"http://127.0.0.1:{service_port}/notes/alpha-note")
    check("service retrieves a single note over HTTP",
          status_code == 200 and note_payload.get("slug") == "alpha-note",
          detail=str(note_payload))
    status_code, component_payload = _http_json(f"http://127.0.0.1:{service_port}/notes/alpha-note/components/transcript")
    check("service exposes component retrieval with length metadata",
          status_code == 200 and component_payload.get("component") == "transcript" and component_payload.get("length", 0) > 0,
          detail=str(component_payload))
    status_code, overwrite_payload = _http_json(
        f"http://127.0.0.1:{service_port}/notes/alpha-note/components/summary",
        method="PUT",
        payload={"value": "Edited summary"},
    )
    check("service overwrites note components over HTTP",
          status_code == 200 and overwrite_payload.get("summary") == "Edited summary",
          detail=str(overwrite_payload))
    status_code, search_payload = _http_json(
        f"http://127.0.0.1:{service_port}/notes/search",
        method="POST",
        payload={"query": "deadlines", "component": "any", "limit": 5},
    )
    check("service deterministic search returns note matches",
          status_code == 200 and len(search_payload.get("results", [])) == 1,
          detail=str(search_payload))

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as service_audio:
        service_audio_path = service_audio.name
    try:
        make_silent_wav(service_audio_path, duration_s=1)
        status_code, job_payload = _http_json(
            f"http://127.0.0.1:{service_port}/jobs/transcribe-file",
            method="POST",
            payload={"path": service_audio_path},
        )
        job_id = job_payload.get("id")
        polled = {}
        for _ in range(30):
            time.sleep(0.05)
            _status, polled = _http_json(f"http://127.0.0.1:{service_port}/jobs/{job_id}")
            if polled.get("status") in ("done", "error"):
                break
        check("service async job completes and returns transcript text",
              polled.get("status") == "done" and "transcript" in polled.get("result", {}),
              detail=str(polled))
    finally:
        os.unlink(service_audio_path)
finally:
    http_server.shutdown()
    http_server.server_close()

# ── 17. Pri1 regression suite ────────────────────────────────────────────────
print("\n[17] Pri1 regression suite")

# 17a. recording-cta-status-consistency: a single helper updates the button
#      label, recording flag, and status_var atomically. We assert the helper
#      exists and references all three of those identifiers, so future drift
#      where _start_recording / _stop_recording set them piecemeal can't
#      reintroduce the CTA-vs-status desync.
gui_source = open(os.path.join(REPO_DIR, "muesli_gui.py"), encoding="utf-8").read()
check("CTA helper _apply_recording_state is defined",
      "def _apply_recording_state" in gui_source)
helper_block_start = gui_source.find("def _apply_recording_state")
helper_block = gui_source[helper_block_start:helper_block_start + 2000]
check("CTA helper updates _recording, _rec_btn, and _status_var atomically",
      "self._recording" in helper_block and "self._rec_btn.configure_button" in helper_block
      and "self._status_var.set" in helper_block,
      detail=helper_block[:200])
check("_start_recording resets state when recorder.start raises",
      "self._apply_recording_state(False, status_text=" in gui_source
      and "Could not start recording" in gui_source)
check("_stop_recording delegates to _apply_recording_state",
      "self._apply_recording_state(False, status_text=\"Finishing processing" in gui_source)

# 17b. transcript-visible-after-stop: the restore call after _detail.show(meta)
#      must NOT be guarded by `if self._live_transcript:`. We assert the gate
#      is gone and the preserved-transcript fallback exists.
check("preserved-transcript fallback exists in _stop_recording",
      "preserved_transcript = self._live_transcript" in gui_source
      and "self._compose_realtime_transcript()" in gui_source)
check("the old `if self._live_transcript:` gate is gone",
      "if self._live_transcript:\n            self._detail.set_live_transcript" not in gui_source)

# 17c. tray-desktop-live-icons: ICO encoder produces a valid Windows .ico file
#      that contains exactly one 32bpp 16x16 image with the documented header
#      structure. We exercise the encoder directly so the test runs without
#      having a tray window up.
import tempfile as _tempfile
sys.path.insert(0, REPO_DIR)
import muesli_hotkey as muesli_hotkey_module
ico_dir = _tempfile.mkdtemp(prefix="muesli-test-ico-")
ico_path = os.path.join(ico_dir, "rec.ico")
muesli_hotkey_module.write_recording_icon(ico_path, size=16, force=True)
ico_bytes = open(ico_path, "rb").read()
check("recording-icon ICO file is created", os.path.exists(ico_path))
check("ICONDIR header is well-formed (reserved=0, type=1, count=1)",
      ico_bytes[:6] == struct.pack("<HHH", 0, 1, 1))
ico_w, ico_h, _, _, planes, bits, byte_size, offset = struct.unpack(
    "<BBBBHHII", ico_bytes[6:22])
check("ICONDIRENTRY reports 16x16, 32bpp, 1 plane",
      ico_w == 16 and ico_h == 16 and planes == 1 and bits == 32,
      detail=f"w={ico_w} h={ico_h} planes={planes} bits={bits}")
check("ICONDIRENTRY image offset and size point inside the file",
      offset + byte_size == len(ico_bytes))
# BITMAPINFOHEADER inside the bitmap region. Height is 2x actual height per ICO spec.
dib_height = struct.unpack("<i", ico_bytes[offset + 8:offset + 12])[0]
check("BITMAPINFOHEADER height is 2x image height (XOR + AND mask convention)",
      dib_height == 32, detail=f"dib_height={dib_height}")
check("write_recording_icon is idempotent when called again without force",
      muesli_hotkey_module.write_recording_icon(ico_path, size=16) == ico_path
      and open(ico_path, "rb").read() == ico_bytes)
import shutil as _shutil
_shutil.rmtree(ico_dir, ignore_errors=True)

# 17d. long-transcript-summary-quality: long transcripts are split into
#      windows, each window is summarised individually, and the per-window
#      briefs are fed back into the real summary prompt — instead of the raw
#      transcript, where the LLM would over-weight the tail.
import muesli_gui as muesli_gui_for_long
long_transcript = "Sentence about the main HDX kickoff topic. " * 200  # ~8000 chars
windows = muesli_gui_for_long._split_transcript_for_map_reduce(long_transcript)
check("long transcripts split into multiple windows for map-reduce",
      len(windows) >= 2, detail=f"window_count={len(windows)}")
short_transcript = "Quick standup notes."
check("short transcripts stay as a single window (no map-reduce overhead)",
      muesli_gui_for_long._split_transcript_for_map_reduce(short_transcript) == [short_transcript])

# Stub _llm_generate so we can verify the call pattern without invoking a real LLM.
generate_calls = []
real_llm = muesli_gui_for_long._llm_generate
def _fake_llm_generate(prompt_text, ollama_timeout=None):
    generate_calls.append(prompt_text)
    if "neutral paragraph" in prompt_text:
        return "This excerpt covers the main HDX kickoff discussion."
    return FAKE_LLM_JSON
muesli_gui_for_long._llm_generate = _fake_llm_generate
try:
    ai = muesli_gui_for_long._generate_ai_fields(long_transcript)
finally:
    muesli_gui_for_long._llm_generate = real_llm
check("map-reduce path issues per-window brief calls + a final summary call",
      len(generate_calls) >= len(windows) + 1,
      detail=f"calls={len(generate_calls)} windows={len(windows)}")
check("the final summary prompt sees the condensed briefs, not the raw tail",
      "Excerpt 1 brief" in generate_calls[-1],
      detail=f"last_call_head={generate_calls[-1][:200]}")
check("map-reduce result is still parsed into the standard ai dict shape",
      isinstance(ai, dict) and "title" in ai and "summary" in ai)

# 17e. pause-processing-semantics: pause/resume helpers live on muesli_runtime
#      (so muesli.py can import them without a GUI dep), HTTP routes exist,
#      MCP tools include the documented natural-language aliases, and the
#      transcribe loop calls wait_for_processing_resume.
import muesli_runtime as muesli_runtime_for_pause
check("muesli_runtime exposes pause/resume helpers",
      callable(getattr(muesli_runtime_for_pause, "is_processing_paused", None))
      and callable(getattr(muesli_runtime_for_pause, "set_processing_paused", None))
      and callable(getattr(muesli_runtime_for_pause, "wait_for_processing_resume", None)))

# Round-trip the pause flag through set/get on a temp runtime state.
# (MUESLI_HOME is already set to TEST_HOME for this test run, so writes are isolated.)
muesli_runtime_for_pause.set_processing_paused(False)
check("set_processing_paused(False) leaves pause unset",
      muesli_runtime_for_pause.is_processing_paused() is False)
muesli_runtime_for_pause.set_processing_paused(True)
check("set_processing_paused(True) sets pause",
      muesli_runtime_for_pause.is_processing_paused() is True)
# wait_for_processing_resume must spin while paused. Inject a fake sleep that
# clears the flag on the second iteration so the loop terminates and we can
# assert it actually polled.
sleep_call_count = [0]
def _fake_sleep(_secs):
    sleep_call_count[0] += 1
    if sleep_call_count[0] >= 2:
        muesli_runtime_for_pause.set_processing_paused(False)
waited = muesli_runtime_for_pause.wait_for_processing_resume(poll_interval=0.0, sleep_fn=_fake_sleep)
check("wait_for_processing_resume polls while paused and returns once unpaused",
      waited is True and sleep_call_count[0] >= 1
      and muesli_runtime_for_pause.is_processing_paused() is False,
      detail=f"sleep_calls={sleep_call_count[0]}")

# muesli.py respects pause inside its transcribe loop.
muesli_source = open(os.path.join(REPO_DIR, "muesli.py"), encoding="utf-8").read()
check("muesli.py imports wait_for_processing_resume from runtime module",
      "from muesli_runtime import wait_for_processing_resume" in muesli_source)
check("muesli.py transcribe loop calls _wait_for_processing_resume between segments",
      muesli_source.count("_wait_for_processing_resume()") >= 2,
      detail=f"count={muesli_source.count('_wait_for_processing_resume()')}")

# HTTP service routes are wired.
service_source = open(os.path.join(REPO_DIR, "muesli_service.py"), encoding="utf-8").read()
check("HTTP route /processing/pause is registered",
      'segments == ["processing", "pause"]' in service_source)
check("HTTP route /processing/resume is registered",
      'segments == ["processing", "resume"]' in service_source)
check("HTTP route /processing/status is registered",
      'segments == ["processing", "status"]' in service_source)

# MCP tools list includes pause/resume/status with the documented NLP aliases.
mcp_source = open(os.path.join(REPO_DIR, "muesli_mcp.py"), encoding="utf-8").read()
check("MCP exposes pause_processing tool",
      'name="pause_processing"' in mcp_source)
check("MCP pause_processing description names the agent aliases",
      "stop the GPU work" in mcp_source and "silence this machine" in mcp_source)
check("MCP exposes resume_processing tool",
      'name="resume_processing"' in mcp_source)
check("MCP exposes processing_status tool",
      'name="processing_status"' in mcp_source)

# 17f. obsidian-export-default: config defaults to True, settings dialog
#      surfaces the toggle, and finished recordings auto-export.
norm = muesli_gui_for_long._normalize_config({})
check("obsidian_auto_export defaults to True in fresh config",
      norm.get("obsidian_auto_export") is True)
norm_off = muesli_gui_for_long._normalize_config({"obsidian_auto_export": False})
check("obsidian_auto_export can be turned off",
      norm_off.get("obsidian_auto_export") is False)
check("Settings dialog surfaces an auto-export checkbox",
      "obsidian_auto_export = tk.BooleanVar" in gui_source
      and "Auto-export every finished recording to Obsidian" in gui_source)
check("Settings dialog _save persists obsidian_auto_export",
      'updated["obsidian_auto_export"] = bool(obsidian_auto_export.get())' in gui_source)
check("_apply_update calls _maybe_auto_export_to_obsidian on done",
      "self._maybe_auto_export_to_obsidian(meta)" in gui_source)

# Functional: _maybe_auto_export_to_obsidian writes a markdown file when the
# vault is configured and the auto-export flag is True. We exercise it by
# constructing a minimal stand-in instead of mounting a Tk root.
class _ObsidianHostStub:
    def __init__(self, status_holder):
        self._status_var = status_holder
    _maybe_auto_export_to_obsidian = muesli_gui_for_long.MuesliApp._maybe_auto_export_to_obsidian

class _Holder:
    def __init__(self): self.last = ""
    def set(self, v): self.last = v

obsidian_vault = _tempfile.mkdtemp(prefix="muesli-test-vault-")
prev_cfg = muesli_gui_for_long.load_config()
try:
    cfg = dict(prev_cfg)
    cfg["obsidian_vault_dir"] = obsidian_vault
    cfg["obsidian_export_folder"] = "Muesli"
    cfg["obsidian_auto_export"] = True
    muesli_gui_for_long.save_config(cfg)
    holder = _Holder()
    host = _ObsidianHostStub(holder)
    fake_meta = {
        "slug": "auto-export-fixture",
        "title": "Auto Export Fixture",
        "started_at": "2026-05-09T10:00:00",
        "duration": 12.5,
        "status": "done",
        "summary": "Body summary.",
        "transcript": "Transcript body.",
        "speakers": 1,
    }
    _ObsidianHostStub._maybe_auto_export_to_obsidian(host, fake_meta)
    expected_note = os.path.join(obsidian_vault, "Muesli", "auto-export-fixture.md")
    check("auto-export writes the markdown note to the configured vault",
          os.path.exists(expected_note),
          detail=f"path={expected_note} status={holder.last!r}")
    check("status_var reports the auto-export result",
          "Auto-exported to Obsidian" in holder.last,
          detail=f"status={holder.last!r}")
    # When auto-export is OFF the note should not be created on the next call.
    cfg["obsidian_auto_export"] = False
    muesli_gui_for_long.save_config(cfg)
    fake_meta2 = dict(fake_meta, slug="auto-export-disabled")
    holder2 = _Holder()
    host2 = _ObsidianHostStub(holder2)
    _ObsidianHostStub._maybe_auto_export_to_obsidian(host2, fake_meta2)
    not_expected = os.path.join(obsidian_vault, "Muesli", "auto-export-disabled.md")
    check("auto-export is skipped when the toggle is off",
          not os.path.exists(not_expected) and holder2.last == "",
          detail=f"path_exists={os.path.exists(not_expected)} status={holder2.last!r}")
finally:
    muesli_gui_for_long.save_config(prev_cfg)
    _shutil.rmtree(obsidian_vault, ignore_errors=True)

print(f"\n{'='*40}")
print(f"  {PASS} passed, {FAIL} failed")
if FAIL:
    sys.exit(1)
