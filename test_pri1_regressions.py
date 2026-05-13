#!/usr/bin/env python3
"""Standalone regression tests for the 2026-05-09 Pri1 batch.

Designed to NOT trigger the heavy import chain (faster-whisper, llama-cpp,
ctranslate2, pygame mixer, etc.). On this PC those imports can take many
minutes or wedge entirely (documented in bugs.md as the [5] transcribe
stall). Avoiding them gives us a fast, deterministic gate for the Pri1 fixes.

Strategy:
  * Source-text assertions cover the structural changes (helper exists,
    old buggy pattern is gone, new code paths are wired in).
  * Functional assertions only use modules that don't pull in the heavy
    chain — `muesli_runtime` (pure stdlib I/O) and `muesli_hotkey` (Win32
    ctypes only, no media imports). These exercise the actual code paths
    for the ICO encoder and the pause-flag round-trip.

The same structural checks live in test_muesli.py [17] for the long-form
suite — keep them in sync if you change one.
"""

from __future__ import annotations

import atexit
import os
import shutil
import struct
import sys
import tempfile

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
TEST_HOME = tempfile.mkdtemp(prefix="muesli-pri1-test-home-")
os.makedirs(os.path.join(TEST_HOME, "shared_audio"), exist_ok=True)
os.environ["MUESLI_HOME"] = TEST_HOME
os.environ["MUESLI_RUNTIME_DIR"] = os.path.join(TEST_HOME, "runtime")
os.makedirs(os.environ["MUESLI_RUNTIME_DIR"], exist_ok=True)
atexit.register(lambda: shutil.rmtree(TEST_HOME, ignore_errors=True))
sys.path.insert(0, REPO_DIR)

# Light imports only — these do NOT pull in whisper / llama / pygame.
import muesli_runtime
import muesli_hotkey

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


def read(name):
    return open(os.path.join(REPO_DIR, name), encoding="utf-8").read()


gui_src = read("muesli_gui.py")
muesli_src = read("muesli.py")
service_src = read("muesli_service.py")
mcp_src = read("muesli_mcp.py")
runtime_src = read("muesli_runtime.py")

print("=== Pri1 regression suite ===\n")


# ── 17a. recording-cta-status-consistency ────────────────────────────────────
print("[17a] recording-cta-status-consistency")
check("CTA helper _apply_recording_state is defined",
      "def _apply_recording_state" in gui_src)
helper_start = gui_src.find("def _apply_recording_state")
helper_block = gui_src[helper_start:helper_start + 2000]
check("CTA helper updates _recording, _rec_btn, and _status_var atomically",
      "self._recording" in helper_block and "self._rec_btn.configure_button" in helper_block
      and "self._status_var.set" in helper_block)
check("_start_recording resets state when recorder.start raises",
      "self._apply_recording_state(False, status_text=" in gui_src
      and "Could not start recording" in gui_src)
check("_stop_recording delegates to _apply_recording_state",
      "self._apply_recording_state(False, status_text=\"Finishing processing" in gui_src)
# The piecemeal updates the helper replaces should be gone from _start_recording.
old_pattern_count = gui_src.count('text="Stop Recording",')
check("piecemeal `text=\"Stop Recording\"` configure_button calls are consolidated",
      old_pattern_count <= 1,
      detail=f"count={old_pattern_count} (expected 1, in the helper)")


# ── 17b. transcript-visible-after-stop ───────────────────────────────────────
print("\n[17b] transcript-visible-after-stop")
check("preserved-transcript fallback exists in _stop_recording",
      "preserved_transcript = self._live_transcript" in gui_src
      and "self._compose_realtime_transcript()" in gui_src)
check("the old `if self._live_transcript:` gate is gone from _stop_recording",
      "if self._live_transcript:\n            self._detail.set_live_transcript" not in gui_src)


# ── 17c. tray-desktop-live-icons ─────────────────────────────────────────────
print("\n[17c] tray-desktop-live-icons")
ico_dir = tempfile.mkdtemp(prefix="muesli-pri1-test-ico-")
try:
    ico_path = os.path.join(ico_dir, "rec.ico")
    muesli_hotkey.write_recording_icon(ico_path, size=16, force=True)
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
    dib_height = struct.unpack("<i", ico_bytes[offset + 8:offset + 12])[0]
    check("BITMAPINFOHEADER height is 2x image height (XOR + AND mask convention)",
          dib_height == 32, detail=f"dib_height={dib_height}")
    check("write_recording_icon is idempotent without force",
          muesli_hotkey.write_recording_icon(ico_path, size=16) == ico_path
          and open(ico_path, "rb").read() == ico_bytes)
finally:
    shutil.rmtree(ico_dir, ignore_errors=True)
# Tray refresh swaps icons based on recording state.
check("_refresh_tray picks _icon_recording when state.recording is true",
      "state.get(\"recording\", False)" in read("muesli_hotkey.py")
      and "self._icon_recording" in read("muesli_hotkey.py"))


# ── 17d. long-transcript-summary-quality ─────────────────────────────────────
print("\n[17d] long-transcript-summary-quality")
check("LONG_TRANSCRIPT_THRESHOLD_CHARS is defined",
      "LONG_TRANSCRIPT_THRESHOLD_CHARS" in gui_src)
check("_split_transcript_for_map_reduce is defined",
      "def _split_transcript_for_map_reduce" in gui_src)
check("_summarize_long_transcript_via_map_reduce is defined",
      "def _summarize_long_transcript_via_map_reduce" in gui_src)
check("_generate_ai_fields branches on transcript length",
      "len(text) > LONG_TRANSCRIPT_THRESHOLD_CHARS" in gui_src)
check("brief prompt directs the LLM to stay neutral and per-excerpt",
      "neutral paragraph" in gui_src and "this excerpt" in gui_src.lower())


# ── 17e. pause-processing-semantics ──────────────────────────────────────────
print("\n[17e] pause-processing-semantics")
check("muesli_runtime exposes pause/resume helpers",
      callable(getattr(muesli_runtime, "is_processing_paused", None))
      and callable(getattr(muesli_runtime, "set_processing_paused", None))
      and callable(getattr(muesli_runtime, "wait_for_processing_resume", None)))

muesli_runtime.set_processing_paused(False)
check("set_processing_paused(False) leaves pause unset",
      muesli_runtime.is_processing_paused() is False)
muesli_runtime.set_processing_paused(True)
check("set_processing_paused(True) sets pause",
      muesli_runtime.is_processing_paused() is True)

sleep_call_count = [0]
def _fake_sleep(_secs):
    sleep_call_count[0] += 1
    if sleep_call_count[0] >= 2:
        muesli_runtime.set_processing_paused(False)
waited = muesli_runtime.wait_for_processing_resume(poll_interval=0.0, sleep_fn=_fake_sleep)
check("wait_for_processing_resume polls while paused and returns once unpaused",
      waited is True and sleep_call_count[0] >= 1
      and muesli_runtime.is_processing_paused() is False,
      detail=f"sleep_calls={sleep_call_count[0]}")
# Reset so no leakage to other tests / real state.
muesli_runtime.set_processing_paused(False)

check("muesli.py imports wait_for_processing_resume from runtime module",
      "from muesli_runtime import wait_for_processing_resume" in muesli_src)
check("muesli.py transcribe loop calls _wait_for_processing_resume between segments",
      muesli_src.count("_wait_for_processing_resume()") >= 2,
      detail=f"count={muesli_src.count('_wait_for_processing_resume()')}")
check("muesli_gui.py delegates _wait_for_processing_resume to runtime module",
      "_wait_for_processing_resume_runtime" in gui_src)

check("HTTP route /processing/pause is registered",
      'segments == ["processing", "pause"]' in service_src)
check("HTTP route /processing/resume is registered",
      'segments == ["processing", "resume"]' in service_src)
check("HTTP route /processing/status is registered",
      'segments == ["processing", "status"]' in service_src)
check("muesli_service imports pause helpers from runtime",
      "from muesli_runtime import is_processing_paused, set_processing_paused" in service_src)

check("MCP exposes pause_processing tool",
      'name="pause_processing"' in mcp_src)
check("MCP pause_processing description names the agent aliases",
      "stop the GPU work" in mcp_src and "silence this machine" in mcp_src)
check("MCP exposes resume_processing tool",
      'name="resume_processing"' in mcp_src)
check("MCP exposes processing_status tool",
      'name="processing_status"' in mcp_src)


# ── 17m. Hotkey launcher prefers pythonw (no CLI window flash at boot) ─────
print("\n[17m] Hotkey launcher prefers pythonw.exe over python.exe")
hotkey_launcher_src = open(os.path.join(REPO_DIR, "muesli_hotkey_launcher.vbs"),
                           encoding="utf-8").read()
check("hotkey launcher defines both pythonExe and pythonwExe paths",
      'pythonExe = venvRoot & "\\Scripts\\python.exe"' in hotkey_launcher_src
      and 'pythonwExe = venvRoot & "\\Scripts\\pythonw.exe"' in hotkey_launcher_src)
check("hotkey launcher upgrades selectedPython to pythonwExe when present",
      'selectedPython = pythonExe' in hotkey_launcher_src
      and 'If fso.FileExists(pythonwExe) Then' in hotkey_launcher_src
      and 'selectedPython = pythonwExe' in hotkey_launcher_src)
check("hotkey launcher invokes selectedPython, not raw pythonExe",
      'Chr(34) & selectedPython & Chr(34)' in hotkey_launcher_src)


# ── 17l. Recorder lazy-inits PyAudio ────────────────────────────────────────
# Without this, Recorder.__init__ called pyaudio.PyAudio() synchronously on
# the Tk main thread at MuesliApp init time — PortAudio enumeration on
# Windows takes 5-15s and can hang on a misbehaving driver, freezing the
# whole GUI on launch. Repro'd 2026-05-12 right after `pip install pyaudio`.
print("\n[17l] Recorder lazy-inits PyAudio (no main-thread block on launch)")
recorder_block_start = gui_src.find("class Recorder:")
recorder_block_end = gui_src.find("\nclass ", recorder_block_start + 1)
recorder_block = gui_src[recorder_block_start:recorder_block_end]
init_block_start = recorder_block.find("def __init__")
init_block_end = recorder_block.find("\n    def ", init_block_start + 1)
init_block = recorder_block[init_block_start:init_block_end]
check("Recorder.__init__ does NOT call pyaudio.PyAudio() (lazy init)",
      "pyaudio.PyAudio()" not in init_block,
      detail=init_block[:300])
check("Recorder.__init__ leaves self._pa = None for lazy population",
      "self._pa        = None" in init_block or "self._pa = None" in init_block)
start_block_start = recorder_block.find("def start(")
start_block_end = recorder_block.find("\n    def ", start_block_start + 1)
start_block = recorder_block[start_block_start:start_block_end]
check("Recorder.start() lazy-inits self._pa on first use",
      "if self._pa is None:" in start_block
      and "self._pa = pyaudio.PyAudio()" in start_block)
check("_startup_background warms PyAudio on a daemon thread",
      "self._recorder._pa = pyaudio.PyAudio()" in gui_src
      and "def _startup_background" in gui_src)


# ── 17k. _effective_summary_runtime is cached ───────────────────────────────
# Without caching, the 1s _poll_runtime_state tick blocks the main thread on
# `_selected_ollama_model() -> _list_ollama_models() -> /api/tags` (3+ seconds
# on a cold ollama). That makes the Tk event loop fall behind, the window
# becomes unresponsive, and clicks (delete, select track) stall. Reproduced
# live on 2026-05-12.
print("\n[17k] _effective_summary_runtime cache prevents poll-blocking")
check("TTL constant + cache slot exist",
      "_EFFECTIVE_SUMMARY_RUNTIME_TTL_S = 30.0" in gui_src
      and "_effective_summary_runtime_cache" in gui_src)
check("compute helper is split out from the cached entry point",
      "def _compute_effective_summary_runtime" in gui_src
      and "def _effective_summary_runtime" in gui_src)
check("cache invalidator exists and is called from _open_settings",
      "def _invalidate_summary_runtime_cache" in gui_src
      and "self._invalidate_summary_runtime_cache()" in gui_src)
check("_effective_summary_runtime returns the cached tuple on TTL hit",
      "(now - cache[0]) < self._EFFECTIVE_SUMMARY_RUNTIME_TTL_S" in gui_src)


# ── 17j. SECURITY: slug + media-path validation ─────────────────────────────
print("\n[17j] SECURITY: slug + media-path validation")
import muesli_service
# Slug validator: real Muesli slugs survive, traversal slugs are rejected.
for legit in ["2026-04-22_10-30-00", "abc-123_xyz", "a", "A1", "x" * 128]:
    try:
        muesli_service._validate_slug(legit)
        check(f"legitimate slug accepted: {legit!r}", True)
    except ValueError as exc:
        check(f"legitimate slug accepted: {legit!r}", False, detail=str(exc))
for evil in [
    "../../etc/passwd",
    "..\\..\\Windows\\System32",
    "/etc/passwd",
    "..",
    "_leadingunderscore",
    "-leadingdash",
    ".hidden",
    "with space",
    "with/slash",
    "with\\backslash",
    "with:colon",
    "x" * 129,  # over max length
    "",
    None,
]:
    rejected = False
    try:
        muesli_service._validate_slug(evil)
    except ValueError:
        rejected = True
    check(f"malicious/invalid slug rejected: {evil!r}", rejected)

# Media-path validator: existence + audio extension + allow-list root.
# The OS temp dir is intentionally allowed (drag-and-drop / download flows
# legitimately land there), so we have to put the rogue fixture somewhere
# outside the entire allow list. Pick the home dir directly — the allow
# list contains ~/Documents, ~/Downloads, etc. but NOT ~ itself, so a
# file at ~/<random> is rejected by os.path.commonpath logic.
import tempfile as _tempfile
mp_root = _tempfile.mkdtemp(prefix="muesli-pri1-rogue-",
                            dir=os.path.expanduser("~"))
try:
    rogue_wav = os.path.join(mp_root, "rogue.wav")
    open(rogue_wav, "wb").write(b"RIFF" + b"\0" * 100)
    rejected = False
    try:
        muesli_service._validate_media_path(rogue_wav)
    except PermissionError:
        rejected = True
    check("media path outside allow-listed roots rejected (PermissionError)",
          rejected, detail=f"path={rogue_wav}")

    # Wrong extension → ValueError, even if inside an allow-listed root.
    home_docs = os.path.join(os.path.expanduser("~"), "Documents")
    if os.path.isdir(home_docs):
        evil_path = os.path.join(home_docs, "muesli-secret-test.txt")
        try:
            open(evil_path, "wb").write(b"sensitive")
            rejected = False
            try:
                muesli_service._validate_media_path(evil_path)
            except ValueError:
                rejected = True
            check("media path with non-audio extension rejected (ValueError)",
                  rejected, detail=f"path={evil_path}")
        finally:
            try: os.remove(evil_path)
            except OSError: pass
    else:
        print("  SKIP  non-audio-extension check (no ~/Documents on this system)")

    # Missing file → FileNotFoundError.
    rejected = False
    try:
        muesli_service._validate_media_path(os.path.join(mp_root, "nope.wav"))
    except FileNotFoundError:
        rejected = True
    check("media path that doesn't exist rejected (FileNotFoundError)", rejected)

    # Empty / None → FileNotFoundError.
    for empty in ["", None, "   "]:
        rejected = False
        try:
            muesli_service._validate_media_path(empty)
        except FileNotFoundError:
            rejected = True
        check(f"empty media path rejected ({empty!r})", rejected)

    # Legitimate audio file inside an allow-listed root → returns realpath.
    if os.path.isdir(home_docs):
        legit_wav = os.path.join(home_docs, "muesli-fixture-ok.wav")
        try:
            open(legit_wav, "wb").write(b"RIFF" + b"\0" * 100)
            resolved = muesli_service._validate_media_path(legit_wav)
            check("legitimate audio file inside allow-listed root accepted",
                  resolved == os.path.realpath(legit_wav),
                  detail=f"resolved={resolved}")
        finally:
            try: os.remove(legit_wav)
            except OSError: pass
finally:
    shutil.rmtree(mp_root, ignore_errors=True)

# Handler integration: the validators are wired into _require_note + DELETE/PUT
# slug arguments + the two job paths. Source-pattern check guards against a
# future refactor accidentally bypassing them.
service_src = read("muesli_service.py")
check("_require_note validates slug",
      "_validate_slug(slug)" in service_src)
check("DELETE /notes/{slug} validates slug",
      "safe_slug = _validate_slug(segments[1])" in service_src
      and "service.delete_note(safe_slug)" in service_src)
check("PUT /notes/{slug}/components/{name} validates slug",
      "service.overwrite_component(safe_slug" in service_src)
check("/jobs/transcribe-file validates path",
      "_validate_media_path(body.get(\"path\"))" in service_src
      and "service.submit_transcribe_file(safe_path)" in service_src)
check("/jobs/ingest-voice-note validates path",
      "service.submit_ingest_voice_note(safe_path)" in service_src)
check("/jobs/reprocess-note validates slug",
      "slug = _validate_slug(body.get(\"slug\"))" in service_src)
check("PermissionError mapped to 403 in all four handlers",
      service_src.count("self._send_json(403, {\"error\": str(exc)})") >= 4)


# ── 17h. recording-state-icon-all-surfaces ───────────────────────────────────
print("\n[17h] recording-state-icon-all-surfaces")
check("_make_recording_variant_icon helper defined in muesli_gui",
      "def _make_recording_variant_icon" in gui_src)
check("MuesliApp.__init__ builds _icon_rec via the variant helper",
      "_make_recording_variant_icon(self, self._icon_idle)" in gui_src)
check("blink timer state vars (_blink_id, _blink_state) initialised",
      "self._blink_id = None" in gui_src and "self._blink_state = False" in gui_src)
check("_start_blink_timer / _tick_blink / _stop_blink_timer all defined",
      "def _start_blink_timer" in gui_src
      and "def _tick_blink" in gui_src
      and "def _stop_blink_timer" in gui_src)
check("_apply_recording_state(True) starts the blink timer",
      "self._start_blink_timer()" in gui_src)
check("_apply_recording_state(False) stops the blink timer",
      "self._stop_blink_timer()" in gui_src)
check("blink cadence is 600ms",
      "self.after(600, self._tick_blink)" in gui_src)
hotkey_src = read("muesli_hotkey.py")
check("tray _refresh_tray alternates icons via _blink_phase when recording",
      "self._blink_phase = not getattr(self, \"_blink_phase\", False)" in hotkey_src
      and "if recording and self._icon_recording:" in hotkey_src)
check("tray pins to idle icon when not recording",
      "self._blink_phase = False" in hotkey_src
      and "desired_icon = self._icon_idle" in hotkey_src)


# ── 17i. process-graph-responsive-layout ─────────────────────────────────────
print("\n[17i] process-graph-responsive-layout")
check("_draw_process_diagram uses actual canvas width with low floor (200, not 420)",
      "max(canvas.winfo_width(), 200)" in gui_src
      and "max(canvas.winfo_width(), 420)" not in gui_src)
check("side margin scales with width (14..34) instead of fixed 34",
      "margin_x = max(14, min(34, int(width * 0.08)))" in gui_src)
# Sanity: the formula gives expected values at narrow / wide.
def _expected_margin(width):
    return max(14, min(34, int(width * 0.08)))
check("at 200px the margin clamps to 16px", _expected_margin(200) == 16,
      detail=f"got {_expected_margin(200)}")
check("at 420px the margin clamps to 33px (just under the 34 ceiling)",
      _expected_margin(420) == 33, detail=f"got {_expected_margin(420)}")
check("at 800px the margin caps at 34px", _expected_margin(800) == 34,
      detail=f"got {_expected_margin(800)}")
check("at 100px the margin floors at 14px", _expected_margin(100) == 14,
      detail=f"got {_expected_margin(100)}")


# ── 17g. discard-short-recordings ────────────────────────────────────────────
print("\n[17g] discard-short-recordings")
check("SHORT_RECORDING_DISCARD_SECONDS constant defined with value 5",
      "SHORT_RECORDING_DISCARD_SECONDS = 5" in gui_src)
check("_discard_short_recording method defined",
      "def _discard_short_recording" in gui_src)
check("_stop_recording branches on duration < threshold",
      'meta.get("duration") or 0) < SHORT_RECORDING_DISCARD_SECONDS' in gui_src)
check("manual title/summary bypasses the discard (has_manual_intent)",
      "has_manual_intent = (" in gui_src
      and "and not has_manual_intent" in gui_src)
check("discard helper calls delete_recording on the meta",
      "delete_recording(meta)" in gui_src[gui_src.find("def _discard_short_recording"):])
check("discard helper resets _chunk_pipeline + _realtime_transcriber to None",
      "self._chunk_pipeline = None" in gui_src[gui_src.find("def _discard_short_recording"):]
      and "self._realtime_transcriber = None" in gui_src[gui_src.find("def _discard_short_recording"):])
# Functional: delete_recording cleans WAV + JSON from REC_DIR. We use the
# import-light helpers from muesli_gui only via a subprocess to avoid pulling
# the heavy chain into THIS test. (The full smoke covers it via [17].)
import subprocess as _sp
fixture = tempfile.mkdtemp(prefix="muesli-discard-fixture-")
try:
    helper = (
        f"import sys, os, json, wave, struct;"
        f"sys.path.insert(0, {REPO_DIR!r});"
        f"os.environ['MUESLI_HOME'] = {fixture!r};"
        "import muesli_gui as mg;"
        "rec_dir = mg.REC_DIR;"
        "os.makedirs(rec_dir, exist_ok=True);"
        "slug = 'discard-fixture';"
        "wav = os.path.join(rec_dir, slug + '.wav');"
        "wf = wave.open(wav, 'wb'); wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(16000);"
        "wf.writeframes(struct.pack('<32000h', *([0]*32000))); wf.close();"
        "meta = {'slug': slug, 'duration': 2.0};"
        "json.dump(meta, open(os.path.join(rec_dir, slug + '.json'), 'w'));"
        "wav_before = os.path.exists(wav);"
        "json_before = os.path.exists(os.path.join(rec_dir, slug + '.json'));"
        "mg.delete_recording(meta);"
        "wav_after = os.path.exists(wav);"
        "json_after = os.path.exists(os.path.join(rec_dir, slug + '.json'));"
        "print(f'before wav={wav_before} json={json_before} | after wav={wav_after} json={json_after}')"
    )
    try:
        proc = _sp.run([sys.executable, "-c", helper], capture_output=True, text=True, timeout=180)
        out_line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        check("delete_recording removes the local WAV and metadata JSON",
              "before wav=True json=True | after wav=False json=False" in out_line,
              detail=f"stdout={out_line!r} stderr={proc.stderr[-300:]!r}")
    except _sp.TimeoutExpired:
        print("  SKIP  delete_recording functional check (import chain stalled — see bugs.md)")
finally:
    shutil.rmtree(fixture, ignore_errors=True)


# ── 17f. obsidian-export-default ─────────────────────────────────────────────
print("\n[17f] obsidian-export-default")
check("config schema includes obsidian_auto_export with True default",
      'normalized["obsidian_auto_export"] = bool(normalized.get("obsidian_auto_export", True))' in gui_src)
check("Settings dialog surfaces an auto-export checkbox",
      "obsidian_auto_export = tk.BooleanVar" in gui_src
      and "Auto-export every finished recording to Obsidian" in gui_src)
check("Settings dialog _save persists obsidian_auto_export",
      'updated["obsidian_auto_export"] = bool(obsidian_auto_export.get())' in gui_src)
check("_apply_update calls _maybe_auto_export_to_obsidian on done",
      "self._maybe_auto_export_to_obsidian(meta)" in gui_src)
check("_maybe_auto_export_to_obsidian is defined and respects the flag",
      "def _maybe_auto_export_to_obsidian" in gui_src
      and 'cfg.get("obsidian_auto_export", True)' in gui_src)
# Functional auto-export — exercise _export_session_to_obsidian directly via a
# subprocess so we can build the markdown file without importing muesli_gui
# at module level here (which would trigger the heavy chain).
import subprocess
fixture_vault = tempfile.mkdtemp(prefix="muesli-pri1-test-vault-")
try:
    helper_script = (
        "import sys, json, os; "
        f"sys.path.insert(0, {REPO_DIR!r}); "
        "import muesli_gui as mg; "
        f"meta = {{'slug': 'pri1-fixture', 'title': 'Pri1 Fixture',"
        f" 'started_at': '2026-05-09T10:00:00', 'duration': 11.0,"
        f" 'status': 'done', 'summary': 'Body summary.',"
        f" 'transcript': 'Transcript body.', 'speakers': 1}}; "
        f"cfg = {{'obsidian_vault_dir': {fixture_vault!r},"
        f" 'obsidian_export_folder': 'Muesli',"
        f" 'obsidian_auto_export': True}}; "
        "note_path = mg._export_session_to_obsidian(meta, cfg); "
        "print(note_path)"
    )
    # Run with a generous timeout but accept that this can hang on cold trees;
    # if so we mark it skipped rather than failing the whole gate.
    try:
        proc = subprocess.run(
            [sys.executable, "-c", helper_script],
            capture_output=True, text=True, timeout=180,
            env=dict(os.environ, MUESLI_HOME=TEST_HOME),
        )
    except subprocess.TimeoutExpired:
        print("  SKIP  _export_session_to_obsidian functional check (import chain stalled — "
              "known issue, see bugs.md)")
    else:
        note_path = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        check("_export_session_to_obsidian writes a markdown file in the vault",
              note_path and os.path.exists(note_path),
              detail=f"stdout={proc.stdout!r} stderr={proc.stderr[-300:]!r}")
        if note_path and os.path.exists(note_path):
            content = open(note_path, encoding="utf-8").read()
            check("exported note contains the title heading and summary section",
                  "# Pri1 Fixture" in content and "## Summary" in content
                  and "Body summary." in content,
                  detail=content[:300])
finally:
    shutil.rmtree(fixture_vault, ignore_errors=True)


print(f"\n{'='*40}")
print(f"  {PASS} passed, {FAIL} failed")
if FAIL:
    sys.exit(1)
