"""Muesli API for audio recording, transcription, and summarisation.

Usage:
    from muesli import Muesli
    m = Muesli()
    m.start_recording()
    session = m.stop_recording()
"""

import threading
import queue
import wave
import subprocess
import os
import json
import time
import datetime
import re

try:
    import sounddevice as sd
except ImportError:
    sd = None

# ── Paths & constants ────────────────────────────────────────────────────────
APP_DIR     = os.environ.get("MUESLI_HOME") or os.path.dirname(os.path.abspath(__file__))
REC_DIR     = os.path.join(APP_DIR, "recordings")
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
PROMPT_FILE = os.path.join(APP_DIR, "prompt.txt")

RATE     = 16000
CHANNELS = 1
CHUNK    = 1024

_READS_PER_CHUNK = int(30 * RATE / CHUNK)  # ~30s of audio per chunk

os.makedirs(REC_DIR, exist_ok=True)


# ── Config ───────────────────────────────────────────────────────────────────
def _load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}


def _get_shared_dir():
    cfg = _load_config()
    local_shared = os.path.join(os.path.expanduser("~"), "Documents", "MuesliData", "analytics", "audio")
    default_shared = (
        local_shared
        if os.name == "nt" else
        "/srv/al/muesli"
    )
    d = cfg.get("shared_dir", default_shared)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


# ── Prompt ───────────────────────────────────────────────────────────────────
_DEFAULT_PROMPT = (
    "Below is a transcript of a recorded conversation.\n\n"
    "{transcript}\n\n"
    "Respond with ONLY a JSON object (no other text, no markdown). "
    "Use exactly these keys:\n"
    '{"title": "1-5 word Title Case name", '
    '"summary": "2-4 sentence summary", '
    '"speakers": 1, '
    '"corrections": "list any likely transcription errors and suggested corrections", '
    '"bugs": "list any software issues or bugs mentioned explicitly in the conversation"}\n'
)


def _get_summary_prompt():
    if os.path.exists(PROMPT_FILE):
        with open(PROMPT_FILE) as f:
            text = f.read().strip()
            if text:
                return text
    return _DEFAULT_PROMPT


# ── Helpers ──────────────────────────────────────────────────────────────────
def _slug_from_title(title):
    s = title.lower().strip()
    s = re.sub(r"[^a-z0-9\s\-]", "", s)
    s = re.sub(r"\s+", "-", s)
    return s[:60] or "recording"


def _datetime_slug():
    return datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def _save_meta(meta):
    path = os.path.join(REC_DIR, meta["slug"] + ".json")
    with open(path, "w") as f:
        json.dump(meta, f, indent=2)


def _load_recording(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _ffmpeg_available():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


# ── Whisper (lazy singleton) ─────────────────────────────────────────────────
_whisper_model = None
_whisper_lock = threading.Lock()


def _whisper_candidates():
    cfg = _load_config()
    model_name = cfg.get("whisper_model", "large-v3")
    force_device = cfg.get("whisper_device", "auto")
    candidates = []

    if force_device in ("auto", "cuda"):
        candidates.append((model_name, "cuda", "float16"))
    if force_device in ("auto", "cpu"):
        candidates.append((model_name, "cpu", "int8"))
    if model_name != "medium":
        candidates.append(("medium", "cpu", "int8"))
    return candidates


def _get_whisper():
    global _whisper_model
    if _whisper_model is None:
        with _whisper_lock:
            if _whisper_model is None:
                from faster_whisper import WhisperModel
                last_error = None
                for model_name, device, compute_type in _whisper_candidates():
                    try:
                        _whisper_model = WhisperModel(
                            model_name,
                            device=device,
                            compute_type=compute_type,
                        )
                        break
                    except Exception as exc:
                        last_error = exc
                if _whisper_model is None:
                    raise RuntimeError(f"Unable to load Whisper model: {last_error}")
    return _whisper_model


# ── LLM (lazy singleton) ─────────────────────────────────────────────────────
_llm_model = None
_llm_lock = threading.Lock()


def _get_llm():
    global _llm_model
    if _llm_model is None:
        with _llm_lock:
            if _llm_model is None:
                from llama_cpp import Llama
                model_dir = os.path.join(APP_DIR, "models")
                model_path = None
                if os.path.isdir(model_dir):
                    for f in sorted(os.listdir(model_dir)):
                        if f.endswith(".gguf"):
                            model_path = os.path.join(model_dir, f)
                            break
                if not model_path:
                    raise FileNotFoundError(f"No .gguf model in {model_dir}")
                _llm_model = Llama(
                    model_path=model_path, n_ctx=4096, n_threads=4, verbose=False
                )
    return _llm_model


def _llm_generate(prompt_text):
    """Run prompt through Claude API first (fast), falling back to local LLM."""
    # Try Claude API first — much faster than CPU inference
    cfg = _load_config()
    api_key = cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=512,
                messages=[{"role": "user", "content": prompt_text}],
            )
            return resp.content[0].text.strip()
        except Exception:
            pass

    # Fallback: local LLM (slow on CPU but works offline)
    try:
        model = _get_llm()
        resp = model.create_chat_completion(
            messages=[{"role": "user", "content": prompt_text}],
            max_tokens=512,
            temperature=0.3,
        )
        return resp["choices"][0]["message"]["content"].strip()
    except Exception:
        pass

    raise RuntimeError("No LLM available (no Claude API key, local model failed)")


# ── Chunk pipeline ───────────────────────────────────────────────────────────
class _ChunkPipeline:
    """Background worker: transcribe + summarise 30s audio chunks."""

    def __init__(self, on_transcribed=None):
        self._transcripts = []
        self._summaries = []
        self._full_text = ""
        self._queue = queue.Queue()
        self._on_transcribed = on_transcribed
        self._done = threading.Event()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def submit(self, wav_path):
        self._queue.put(wav_path)

    def finish(self):
        self._queue.put(None)
        self._done.wait()

    def _run(self):
        whisper = _get_whisper()
        while True:
            path = self._queue.get()
            if path is None:
                self._done.set()
                return
            # Transcribe
            try:
                segments, _ = whisper.transcribe(path, beam_size=5)
                text = " ".join(s.text.strip() for s in segments).strip()
            except Exception:
                text = ""
            self._transcripts.append(text)
            try:
                os.remove(path)
            except OSError:
                pass

            if text:
                self._full_text = (self._full_text + " " + text).strip()

            n = len(self._transcripts)
            if self._on_transcribed:
                self._on_transcribed(n, self._full_text)

            # Summarise chunk
            if text:
                try:
                    summary = _llm_generate(
                        "Summarise this conversation excerpt in 1-2 sentences. "
                        "Also note how many distinct speakers you detect.\n\n" + text
                    )
                    self._summaries.append(summary)
                except Exception:
                    self._summaries.append("")
            else:
                self._summaries.append("")

    @property
    def transcript(self):
        return " ".join(t for t in self._transcripts if t)

    @property
    def summaries(self):
        return list(self._summaries)


# ── Core Muesli class ────────────────────────────────────────────────────────
class Muesli:
    """Importable API for audio recording, transcription, and summarisation."""

    def __init__(self, auto_resume=False):
        self._shared_dir = _get_shared_dir()
        self._recording = False
        self._pa = None
        self._stream = None
        self._frames = []
        self._rec_thread = None
        self._rec_slug = None
        self._rec_started = None
        self._pipeline = None
        self._chunk_idx = 0
        self._record_backend = None
        self._sample_width = 2
        if auto_resume:
            self.resume_interrupted()

    # ── Resume interrupted sessions ──────────────────────────────────────────

    def _find_audio_for_slug(self, slug):
        """Locate audio for a session slug. Checks REC_DIR (wav) and shared dir (mp3/wav)."""
        # WAV in recordings dir (not yet converted)
        p = os.path.join(REC_DIR, slug + ".wav")
        if os.path.exists(p):
            return p
        # Already converted to shared dir
        for ext in (".mp3", ".wav"):
            p = os.path.join(self._shared_dir, slug + ext)
            if os.path.exists(p):
                return p
        return None

    def resume_interrupted(self, on_progress=None):
        """Find sessions stuck in processing/error-interrupted and resume them.

        Args:
            on_progress: optional callback(slug, stage) for status updates.
                         stage is "resuming", "transcribing", "summarising", "done", or "no_audio".

        Returns:
            list of completed session dicts (only those that had audio to resume).
        """
        resumed = []
        for fname in os.listdir(REC_DIR):
            if not fname.endswith(".json"):
                continue
            meta = _load_recording(os.path.join(REC_DIR, fname))
            if not meta:
                continue
            status = meta.get("status", "")
            error = meta.get("error", "")
            # Resume if stuck processing, or if it was flagged as interrupted
            if status == "processing" or (status == "error" and "Interrupted" in error):
                slug = meta.get("slug", "")
                audio = self._find_audio_for_slug(slug)
                if not audio:
                    if on_progress:
                        on_progress(slug, "no_audio")
                    continue
                if on_progress:
                    on_progress(slug, "resuming")
                try:
                    session = self._resume_session(meta, audio, on_progress)
                    resumed.append(session)
                except Exception as e:
                    meta["status"] = "error"
                    meta["error"] = f"Resume failed: {str(e)[:200]}"
                    _save_meta(meta)
        return resumed

    def _resume_session(self, meta, audio_path, on_progress=None):
        """Re-process a single interrupted session from its audio file."""
        slug = meta.get("slug", "")

        # Transcribe with progress
        transcript = meta.get("transcript") or None
        if not transcript:
            if on_progress:
                on_progress(slug, "transcribing 0%")
            transcript = self.transcribe_with_progress(
                audio_path,
                on_percent=lambda pct: on_progress(slug, f"transcribing {pct}%") if on_progress else None,
            )

        # The audio file for _finalize: prefer the WAV in REC_DIR (it handles conversion)
        wav_in_rec = os.path.join(REC_DIR, slug + ".wav")
        finalize_path = wav_in_rec if os.path.exists(wav_in_rec) else audio_path

        if on_progress:
            on_progress(slug, "summarising")
        meta = self._finalize(meta, finalize_path, transcript, summaries=None)
        if on_progress:
            on_progress(slug, "done")
        return meta

    # ── Recording ────────────────────────────────────────────────────────────

    def start_recording(self, on_transcribed=None):
        """Begin recording from the default microphone.

        Args:
            on_transcribed: optional callback(chunk_num, text_so_far) called
                            each time a 30s chunk is transcribed.
        """
        if self._recording:
            raise RuntimeError("Already recording")
        try:
            import pyaudio
            self._pa = pyaudio.PyAudio()
            self._record_backend = "pyaudio"
            self._sample_width = self._pa.get_sample_size(pyaudio.paInt16)
        except ImportError:
            if sd is None:
                raise RuntimeError("No recording backend is available")
            self._record_backend = "sounddevice"
            self._sample_width = 2
        self._rec_slug = _datetime_slug()
        self._frames = []
        self._chunk_idx = 0
        self._recording = True
        self._rec_started = time.time()

        self._pipeline = _ChunkPipeline(on_transcribed=on_transcribed)
        if self._record_backend == "pyaudio":
            import pyaudio
            self._stream = self._pa.open(
                format=pyaudio.paInt16,
                channels=CHANNELS,
                rate=RATE,
                input=True,
                frames_per_buffer=CHUNK,
            )
            self._rec_thread = threading.Thread(target=self._record_loop, daemon=True)
            self._rec_thread.start()
        else:
            self._stream = sd.InputStream(
                samplerate=RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK,
                callback=self._record_sounddevice,
            )
            self._stream.start()

    def _record_sounddevice(self, indata, frames, time_info, status):
        if not self._recording:
            return
        self._frames.append(indata.copy().tobytes())

    def _record_loop(self):
        chunk_frames = []
        while self._recording:
            data = self._stream.read(CHUNK, exception_on_overflow=False)
            self._frames.append(data)
            chunk_frames.append(data)
            if len(chunk_frames) >= _READS_PER_CHUNK:
                self._emit_chunk(chunk_frames)
                chunk_frames = []
        # Emit remaining frames
        if chunk_frames:
            self._emit_chunk(chunk_frames)

    def _emit_chunk(self, frames):
        import pyaudio
        self._chunk_idx += 1
        path = os.path.join(REC_DIR, f"{self._rec_slug}_chunk{self._chunk_idx}.wav")
        wf = wave.open(path, "wb")
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(self._pa.get_sample_size(pyaudio.paInt16))
        wf.setframerate(RATE)
        wf.writeframes(b"".join(frames))
        wf.close()
        self._pipeline.submit(path)

    def recording_elapsed(self):
        """Seconds elapsed since start_recording(), or 0."""
        if self._rec_started:
            return time.time() - self._rec_started
        return 0

    def stop_recording(self):
        """Stop recording, wait for processing, return session dict.

        Blocks until transcription, summarisation, and audio conversion are done.
        Returns the full session dict (same shape as JSON metadata, plus audio_path).
        """
        if not self._recording:
            raise RuntimeError("Not recording")

        self._recording = False
        if self._rec_thread:
            self._rec_thread.join(timeout=10)
        if self._stream:
            if self._record_backend == "pyaudio":
                self._stream.stop_stream()
                self._stream.close()
            else:
                self._stream.stop()
                self._stream.close()

        duration = time.time() - self._rec_started

        # Save full WAV
        slug = self._rec_slug
        wav_path = os.path.join(REC_DIR, slug + ".wav")
        wf = wave.open(wav_path, "wb")
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(self._sample_width)
        wf.setframerate(RATE)
        wf.writeframes(b"".join(self._frames))
        wf.close()

        meta = {
            "slug": slug,
            "title": slug,
            "started_at": datetime.datetime.now().isoformat(),
            "duration": duration,
            "status": "processing",
            "summary": "",
            "transcript": "",
            "speakers": 0,
        }
        _save_meta(meta)

        # Wait for chunk pipeline
        self._pipeline.finish()
        transcript = self._pipeline.transcript.strip()
        summaries = self._pipeline.summaries

        # Process: convert audio + final summarise
        meta = self._finalize(meta, wav_path, transcript or None, summaries)

        # Cleanup
        if self._pa:
            self._pa.terminate()
        self._pa = None
        self._record_backend = None
        self._pipeline = None
        self._rec_started = None

        return meta

    def _finalize(self, meta, wav_path, transcript, summaries):
        """Convert audio, run final summary, save everything. Returns updated meta."""
        slug = meta["slug"]
        mp3_path = os.path.join(self._shared_dir, slug + ".mp3")
        wav_dest = os.path.join(self._shared_dir, slug + ".wav")

        # WAV -> MP3
        if not os.path.exists(mp3_path) and not os.path.exists(wav_dest):
            if _ffmpeg_available():
                try:
                    subprocess.run(
                        ["ffmpeg", "-y", "-i", wav_path,
                         "-ac", "1", "-ar", "16000", "-b:a", "64k", mp3_path],
                        capture_output=True, check=True,
                    )
                    if os.path.exists(wav_path):
                        os.remove(wav_path)
                except subprocess.CalledProcessError:
                    # Fall back to WAV copy
                    import shutil
                    shutil.copy2(wav_path, wav_dest)
            else:
                import shutil
                shutil.copy2(wav_path, wav_dest)
                if os.path.exists(wav_path):
                    os.remove(wav_path)

        # Final summary
        try:
            if summaries and any(summaries):
                merged = "\n".join(f"- {s}" for s in summaries if s)
                prompt_text = _get_summary_prompt().replace(
                    "{transcript}",
                    f"[Chunk summaries from a longer recording]\n{merged}",
                )
            else:
                prompt_text = _get_summary_prompt().replace("{transcript}", transcript or "")
            raw = _llm_generate(prompt_text)
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
            ai = json.loads(raw)
        except Exception:
            ai = {"title": "Recording", "summary": "", "speakers": 1}

        title = ai.get("title", "Recording").strip() or "Recording"
        summary = ai.get("summary", "")
        speakers = int(ai.get("speakers", 1))
        corrections = ai.get("corrections", "")
        bugs = ai.get("bugs", "")

        # Build final slug from title
        new_slug = _slug_from_title(title)
        candidate = new_slug
        i = 2
        while (os.path.exists(os.path.join(REC_DIR, candidate + ".json"))
               and candidate != slug):
            candidate = f"{new_slug}-{i}"
            i += 1
        new_slug = candidate

        # Rename audio if slug changed
        if new_slug != slug:
            for ext in (".mp3", ".wav"):
                old_a = os.path.join(self._shared_dir, slug + ext)
                new_a = os.path.join(self._shared_dir, new_slug + ext)
                if os.path.exists(old_a):
                    os.rename(old_a, new_a)
                    break

        # Write markdown transcript
        txt_path = os.path.join(self._shared_dir, new_slug + ".txt")
        txt_body = f"# {title}\n\n## Summary\n\n{summary}\n\n"
        if corrections:
            txt_body += f"## Transcription Corrections\n\n{corrections}\n\n"
        if bugs:
            txt_body += f"## Bugs / Issues Mentioned\n\n{bugs}\n\n"
        txt_body += f"## Transcript\n\n{transcript}\n"
        with open(txt_path, "w") as f:
            f.write(txt_body)

        # Remove old metadata if slug changed
        old_json = os.path.join(REC_DIR, slug + ".json")
        if new_slug != slug and os.path.exists(old_json):
            os.remove(old_json)

        meta.update({
            "slug": new_slug,
            "title": title,
            "summary": summary,
            "transcript": transcript or "",
            "speakers": speakers,
            "corrections": corrections,
            "bugs": bugs,
            "status": "done",
            "audio_path": self.audio_path(new_slug),
        })
        meta.pop("error", None)
        _save_meta(meta)
        return meta

    # ── Process existing file ────────────────────────────────────────────────

    def process_file(self, audio_file_path):
        """Transcribe and summarise an existing audio file (WAV or MP3).

        Returns the full session dict.
        """
        if not os.path.exists(audio_file_path):
            raise FileNotFoundError(audio_file_path)

        slug = _datetime_slug()
        # Copy source to shared dir
        ext = os.path.splitext(audio_file_path)[1].lower()
        if ext not in (".wav", ".mp3"):
            ext = ".wav"

        import shutil
        dest = os.path.join(self._shared_dir, slug + ext)
        shutil.copy2(audio_file_path, dest)

        # If WAV, also keep a copy in REC_DIR for processing
        wav_path = None
        if ext == ".wav":
            wav_path = os.path.join(REC_DIR, slug + ".wav")
            shutil.copy2(audio_file_path, wav_path)

        # Get duration via ffprobe if available
        duration = self._probe_duration(audio_file_path)

        meta = {
            "slug": slug,
            "title": slug,
            "started_at": datetime.datetime.now().isoformat(),
            "duration": duration,
            "status": "processing",
            "summary": "",
            "transcript": "",
            "speakers": 0,
        }
        _save_meta(meta)

        # Transcribe
        transcript = self.transcribe(audio_file_path)

        # Finalise (summary, rename, save)
        meta = self._finalize(
            meta,
            wav_path or audio_file_path,
            transcript,
            summaries=None,
        )
        return meta

    def _probe_duration(self, path):
        """Get audio duration in seconds via ffprobe, or 0."""
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries",
                 "format=duration", "-of", "csv=p=0", path],
                capture_output=True, text=True, check=True,
            )
            return float(r.stdout.strip())
        except Exception:
            return 0

    # ── Transcribe ───────────────────────────────────────────────────────────

    def transcribe(self, audio_file_path):
        """Transcribe an audio file with Whisper. Returns plain text."""
        return self.transcribe_with_progress(audio_file_path)

    def transcribe_with_progress(self, audio_file_path, on_percent=None):
        """Transcribe with optional progress callback. on_percent(int) called with 0-100."""
        model = _get_whisper()
        segments, info = model.transcribe(audio_file_path, beam_size=5)
        total_duration = info.duration if info.duration else 0
        collected = []
        last_pct = -1
        for seg in segments:
            collected.append(seg)
            if on_percent and total_duration > 0:
                pct = min(int(seg.end / total_duration * 100), 99)
                if pct != last_pct:
                    on_percent(pct)
                    last_pct = pct
        if on_percent:
            on_percent(100)
        return " ".join(s.text.strip() for s in collected).strip()

    # ── Summarize ────────────────────────────────────────────────────────────

    def summarize(self, transcript):
        """Summarise a transcript string. Returns dict with title, summary, speakers, etc."""
        prompt_text = _get_summary_prompt().replace("{transcript}", transcript)
        raw = _llm_generate(prompt_text)
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"title": "Recording", "summary": raw, "speakers": 1}

    # ── Session access ───────────────────────────────────────────────────────

    def list_sessions(self):
        """List all saved sessions, newest first. Each is a dict."""
        results = []
        for fname in os.listdir(REC_DIR):
            if fname.endswith(".json"):
                m = _load_recording(os.path.join(REC_DIR, fname))
                if m:
                    m["audio_path"] = self.audio_path(m.get("slug", ""))
                    results.append(m)
        results.sort(key=lambda m: m.get("started_at", ""), reverse=True)
        return results

    def get_session(self, slug):
        """Load a single session by slug. Returns dict or None."""
        path = os.path.join(REC_DIR, slug + ".json")
        m = _load_recording(path)
        if m:
            m["audio_path"] = self.audio_path(slug)
        return m

    def audio_path(self, slug):
        """Return path to the audio file (MP3 preferred, WAV fallback), or None."""
        for ext in (".mp3", ".wav"):
            p = os.path.join(self._shared_dir, slug + ext)
            if os.path.exists(p):
                return p
        return None

    def audio_exists(self, slug):
        """Check if audio exists for a given session slug."""
        return self.audio_path(slug) is not None
