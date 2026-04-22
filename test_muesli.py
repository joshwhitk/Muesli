#!/home/algorithm/venv/bin/python3
"""Quick smoke test for the muesli module API."""

import sys, os, wave, struct, tempfile, time
# Unbuffered stdout so progress appears immediately when piped
sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from muesli import Muesli

PASS = 0
FAIL = 0

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


print("=== muesli module test ===\n")

# ── 1. Init ──────────────────────────────────────────────────────────────────
print("[1] Init")
m = Muesli()
check("Muesli() creates instance", m is not None)
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

# ── 7. process_file (silent wav) ─────────────────────────────────────────────
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
    print(f"       slug: {session.get('slug')}")
    print(f"       title: {session.get('title')}")
finally:
    os.unlink(tmp_wav) if os.path.exists(tmp_wav) else None

# ── 8. _resume_session (test with a tiny fake interrupted session) ────────────
print("\n[8] resume_session")
# Create a fake interrupted session with a tiny audio file
_test_slug = "_test_resume_check"
_test_wav = os.path.join("/home/algorithm/granola/recordings", _test_slug + ".wav")
_test_json = os.path.join("/home/algorithm/granola/recordings", _test_slug + ".json")
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
with open(_test_json, "w") as _f:
    _json.dump(_meta, _f)
# Resume just this one session directly (skip resume_interrupted which would hit all)
progress_log = []
result = m._resume_session(_meta, _test_wav,
    on_progress=lambda slug, stage: progress_log.append((slug, stage)))
check("_resume_session returns dict", isinstance(result, dict))
check("_resume_session status=done", result.get("status") == "done",
      f"status={result.get('status')}, error={result.get('error')}")
check("_resume_session has transcript", "transcript" in result)
check("_resume_session progress callbacks fired", len(progress_log) >= 2,
      f"got {progress_log}")
print(f"       slug: {result.get('slug')}, events: {progress_log}")
# Clean up
for _p in [_test_json, _test_wav]:
    if os.path.exists(_p):
        os.remove(_p)
for _d in ["/home/algorithm/granola/recordings", m._shared_dir]:
    for _f in os.listdir(_d):
        if "_test" in _f or _f.startswith("recording") and _f.endswith((".json", ".wav", ".mp3", ".txt")):
            try: os.remove(os.path.join(_d, _f))
            except: pass

# ── Summary ──────────────────────────────────────────────────────────────────
print(f"\n{'='*40}")
print(f"  {PASS} passed, {FAIL} failed")
if FAIL:
    sys.exit(1)
