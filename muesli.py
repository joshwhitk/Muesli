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
import ctypes
import shutil
import sys
import importlib.util
import urllib.request
import urllib.error
import uuid

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
LIVE_CHUNK_SECONDS = 10
OPENAI_TRANSCRIPTION_DEFAULT_MODEL = "gpt-4o-transcribe"
OPENAI_TRANSCRIPTION_MODELS = (
    OPENAI_TRANSCRIPTION_DEFAULT_MODEL,
    "gpt-4o-mini-transcribe",
)
OPENAI_TRANSCRIPTION_TIMEOUT_SEC = 900
_READS_PER_CHUNK = int(LIVE_CHUNK_SECONDS * RATE / CHUNK)

os.makedirs(REC_DIR, exist_ok=True)

WHISPER_QUALITY_MODELS = {
    "fast": "medium",
    "high": "large-v3",
}
_MANAGED_WHISPER_MODELS = set(WHISPER_QUALITY_MODELS.values())
HIGH_QUALITY_MIN_FREE_GB = 20
_CUDA_RUNTIME_PREPPED = False


# ── Config ───────────────────────────────────────────────────────────────────
def _load_config():
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
    return _normalize_config(cfg)


def _free_disk_gb(path=None):
    base = path or os.path.expanduser("~")
    try:
        return shutil.disk_usage(base).free / (1024 ** 3)
    except OSError:
        return 0.0


def _default_whisper_quality():
    return "high" if _free_disk_gb() >= HIGH_QUALITY_MIN_FREE_GB else "fast"


def _infer_whisper_quality(model_name):
    model_name = str(model_name or "").lower()
    if model_name in ("large", "large-v2", "large-v3", "turbo"):
        return "high"
    return "fast"


def _normalize_config(cfg):
    normalized = dict(cfg or {})
    if not normalized.get("launch_hotkey"):
        normalized["launch_hotkey"] = "Ctrl+Shift+`"

    quality = normalized.get("whisper_quality")
    model_name = normalized.get("whisper_model")
    if quality not in WHISPER_QUALITY_MODELS:
        quality = _infer_whisper_quality(model_name) if model_name else _default_whisper_quality()
    normalized["whisper_quality"] = quality
    if not model_name or model_name in _MANAGED_WHISPER_MODELS:
        normalized["whisper_model"] = WHISPER_QUALITY_MODELS[quality]

    whisper_device = str(normalized.get("whisper_device", "auto")).lower()
    normalized["whisper_device"] = whisper_device if whisper_device in ("auto", "cpu", "cuda") else "auto"

    transcription_backend = str(normalized.get("transcription_backend", "local")).lower()
    normalized["transcription_backend"] = transcription_backend if transcription_backend in ("local", "openai", "openai_realtime") else "local"
    openai_model = str(normalized.get("openai_transcription_model", OPENAI_TRANSCRIPTION_DEFAULT_MODEL)).strip()
    normalized["openai_transcription_model"] = openai_model if openai_model in OPENAI_TRANSCRIPTION_MODELS else OPENAI_TRANSCRIPTION_DEFAULT_MODEL
    normalized["openai_api_key"] = str(normalized.get("openai_api_key", "")).strip()

    backend = str(normalized.get("llm_backend", "auto")).lower()
    normalized["llm_backend"] = backend if backend in ("auto", "anthropic", "local") else "auto"
    recording_backend = str(normalized.get("audio_input_backend", "auto")).lower()
    normalized["audio_input_backend"] = recording_backend if recording_backend in ("auto", "pyaudio", "sounddevice") else "auto"
    normalized["audio_input_device"] = str(normalized.get("audio_input_device", "")).strip()
    legacy_prompt = _read_legacy_prompt() if "summary_modes" not in normalized else ""
    normalized["summary_modes"] = _coerce_summary_modes(normalized.get("summary_modes"), legacy_prompt)
    active_mode = str(normalized.get("active_summary_mode") or SUMMARY_MODE_GENERAL_ID).strip().lower()
    if active_mode not in {mode["id"] for mode in normalized["summary_modes"]}:
        active_mode = SUMMARY_MODE_GENERAL_ID
    normalized["active_summary_mode"] = active_mode
    return normalized


def _get_shared_dir():
    cfg = _load_config()
    local_shared = os.path.join(os.path.expanduser("~"), "Documents", "MuesliData", "analytics", "audio")
    default_shared = (
        local_shared
        if os.name == "nt" else
        "/srv/muesli"
    )
    d = cfg.get("shared_dir", default_shared)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def _preferred_recording_backend():
    if importlib.util.find_spec("pyaudio") is not None:
        return "pyaudio"
    if sd is not None:
        return "sounddevice"
    return ""


def _list_audio_input_devices(backend=None):
    backend = backend or _preferred_recording_backend()
    options = [{
        "id": "",
        "label": "System Default",
        "backend": backend,
        "name": "System Default",
    }]
    if backend == "pyaudio":
        try:
            import pyaudio
        except ImportError:
            return options
        pa = pyaudio.PyAudio()
        try:
            try:
                default_index = int(pa.get_default_input_device_info().get("index"))
            except Exception:
                default_index = None
            for idx in range(pa.get_device_count()):
                try:
                    info = pa.get_device_info_by_index(idx)
                except Exception:
                    continue
                if int(info.get("maxInputChannels", 0) or 0) <= 0:
                    continue
                label = str(info.get("name") or f"Input {idx}").strip()
                try:
                    host_name = pa.get_host_api_info_by_index(int(info.get("hostApi", 0))).get("name", "")
                except Exception:
                    host_name = ""
                if host_name:
                    label += f" ({host_name})"
                if default_index == idx:
                    label += " [Default]"
                options.append({
                    "id": str(idx),
                    "label": label,
                    "backend": "pyaudio",
                    "name": str(info.get("name") or label),
                })
        finally:
            pa.terminate()
    elif backend == "sounddevice" and sd is not None:
        try:
            devices = sd.query_devices()
            hostapis = sd.query_hostapis()
            default_raw = sd.default.device
            default_index = default_raw[0] if isinstance(default_raw, (list, tuple)) else default_raw
        except Exception:
            devices = []
            hostapis = []
            default_index = None
        for idx, info in enumerate(devices):
            if int(info.get("max_input_channels", 0) or 0) <= 0:
                continue
            label = str(info.get("name") or f"Input {idx}").strip()
            host_name = ""
            try:
                host_name = hostapis[int(info.get("hostapi", 0))].get("name", "")
            except Exception:
                host_name = ""
            if host_name:
                label += f" ({host_name})"
            if default_index == idx:
                label += " [Default]"
            options.append({
                "id": str(idx),
                "label": label,
                "backend": "sounddevice",
                "name": str(info.get("name") or label),
            })
    return options


def _resolve_audio_input_device(backend):
    cfg = _load_config()
    device_id = str(cfg.get("audio_input_device", "")).strip()
    if not device_id:
        return None
    configured_backend = str(cfg.get("audio_input_backend", "auto")).lower()
    effective_backend = _preferred_recording_backend() if configured_backend == "auto" else configured_backend
    if effective_backend != backend:
        return None
    options = _list_audio_input_devices(backend)
    valid_ids = {item["id"] for item in options}
    if device_id not in valid_ids:
        return None
    try:
        return int(device_id)
    except ValueError:
        return None


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

SUMMARY_MODE_GENERAL_ID = "general"


def _summary_prompt(extra_instructions):
    return (
        "Below is a transcript of a recorded conversation.\n\n"
        "{transcript}\n\n"
        "Respond with ONLY a JSON object (no other text, no markdown). "
        "Use exactly these keys:\n"
        '{"title": "1-5 word Title Case name", '
        '"summary": "2-4 sentence summary", '
        '"speakers": 1, '
        '"corrections": "list any likely transcription errors and suggested corrections", '
        '"bugs": "list any software issues or bugs mentioned explicitly in the conversation"}\n'
        f"{extra_instructions}\n"
    )


_DEFAULT_SUMMARY_MODES = [
    {"id": SUMMARY_MODE_GENERAL_ID, "title": "General", "prompt": _DEFAULT_PROMPT},
    {
        "id": "work_meeting",
        "title": "Work Meeting",
        "prompt": _summary_prompt(
            "Optimize for work meetings. Emphasize decisions, owners, deadlines, blockers, and follow-up actions."
        ),
    },
    {
        "id": "user_research",
        "title": "User Research",
        "prompt": _summary_prompt(
            "Optimize for user research. Emphasize participant goals, pain points, notable quotes, observed behavior, and product insights."
        ),
    },
    {
        "id": "accountability_call",
        "title": "Accountability Call",
        "prompt": _summary_prompt(
            "Optimize for an accountability call. Emphasize commitments, promises, deadlines, progress updates, risks, and next check-in items."
        ),
    },
    {
        "id": "negotiation_agreement_call",
        "title": "Negotiation Agreement Call",
        "prompt": _summary_prompt(
            "Optimize for a negotiation or agreement call. Emphasize terms discussed, points of alignment, open issues, concessions, and explicit commitments."
        ),
    },
]


def _read_legacy_prompt():
    if os.path.exists(PROMPT_FILE):
        try:
            with open(PROMPT_FILE, encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            return ""
    return ""


def _default_summary_modes(legacy_general_prompt=None):
    modes = [dict(mode) for mode in _DEFAULT_SUMMARY_MODES]
    if legacy_general_prompt:
        modes[0]["prompt"] = legacy_general_prompt
    return modes


def _coerce_summary_modes(raw_modes, legacy_general_prompt=None):
    defaults = _default_summary_modes(legacy_general_prompt)
    normalized = []
    seen_ids = set()
    general_prompt = legacy_general_prompt or defaults[0]["prompt"]
    if isinstance(raw_modes, list):
        for idx, item in enumerate(raw_modes):
            if not isinstance(item, dict):
                continue
            mode_id = str(item.get("id") or "").strip().lower()
            title = str(item.get("title") or "").strip()
            prompt = str(item.get("prompt") or "").strip()
            if not prompt:
                continue
            if mode_id == SUMMARY_MODE_GENERAL_ID:
                general_prompt = prompt
            if not mode_id:
                mode_id = f"custom_{idx + 1}"
            if mode_id in seen_ids:
                continue
            seen_ids.add(mode_id)
            normalized.append({
                "id": mode_id,
                "title": "General" if mode_id == SUMMARY_MODE_GENERAL_ID else (title or f"Mode {idx + 1}"),
                "prompt": prompt,
            })
    result = [{
        "id": SUMMARY_MODE_GENERAL_ID,
        "title": "General",
        "prompt": general_prompt,
    }]
    seen = {SUMMARY_MODE_GENERAL_ID}
    for item in normalized:
        if item["id"] == SUMMARY_MODE_GENERAL_ID or item["id"] in seen:
            continue
        seen.add(item["id"])
        result.append(item)
    default_ids = {mode["id"] for mode in result}
    for item in defaults[1:]:
        if item["id"] not in default_ids:
            result.append(dict(item))
    return result


def _get_summary_mode(mode_id=None):
    cfg = _load_config()
    modes = cfg.get("summary_modes", _default_summary_modes())
    active_id = str(mode_id or cfg.get("active_summary_mode") or SUMMARY_MODE_GENERAL_ID).strip().lower()
    for mode in modes:
        if mode.get("id") == active_id:
            return dict(mode)
    return dict(modes[0])


def _get_summary_prompt(mode_id=None):
    return _get_summary_mode(mode_id).get("prompt", _DEFAULT_PROMPT)


# ── Helpers ──────────────────────────────────────────────────────────────────
def _slug_from_title(title):
    s = title.lower().strip()
    s = re.sub(r"[^a-z0-9\s\-]", "", s)
    s = re.sub(r"\s+", "-", s)
    return s[:60] or "recording"


def _default_session_title(started_at):
    try:
        dt = datetime.datetime.fromisoformat(started_at)
        return dt.strftime("%Y-%m-%d %I-%M %p").replace(" 0", " ")
    except Exception:
        return "Recording"


def _clean_sentence(text):
    text = re.sub(r"\s+", " ", (text or "")).strip()
    return text.strip(" -,:;")


def _fallback_ai_fields(transcript):
    return {
        "title": "",
        "summary": "",
        "speakers": 0,
        "corrections": "",
        "bugs": "",
    }


def _apply_manual_note_overrides(meta, title, summary):
    if bool(meta.get("title_manual")):
        manual_title = str(meta.get("title") or "").strip()
        if manual_title:
            title = manual_title
    if bool(meta.get("summary_manual")):
        summary = str(meta.get("summary") or "").strip()
    return title, summary


def _datetime_slug():
    return datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def _save_meta(meta):
    path = os.path.join(REC_DIR, meta["slug"] + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


def _load_recording(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _ffmpeg_available():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True, **_subprocess_kwargs())
        return True
    except Exception:
        return False


def _subprocess_kwargs():
    if os.name != "nt":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _normalise_import_audio(source_path, slug, shared_dir):
    """Return (import_path, staged_ext, rec_wav_path) for an imported audio file.

    Native WAV and MP3 files keep their original container. Other formats are
    transcoded with ffmpeg into a real WAV so downstream Whisper/ffmpeg steps do
    not operate on mislabeled files.
    """
    ext = os.path.splitext(source_path)[1].lower()
    import shutil

    if ext in (".wav", ".mp3"):
        dest = os.path.join(shared_dir, slug + ext)
        shutil.copy2(source_path, dest)
        wav_path = None
        if ext == ".wav":
            wav_path = os.path.join(REC_DIR, slug + ".wav")
            shutil.copy2(source_path, wav_path)
        return source_path, ext, wav_path

    if not _ffmpeg_available():
        raise RuntimeError(f"Unsupported import format {ext or '<none>'} without ffmpeg")

    wav_path = os.path.join(REC_DIR, slug + ".wav")
    subprocess.run(
        ["ffmpeg", "-y", "-i", source_path, "-ac", "1", "-ar", "16000", wav_path],
        capture_output=True,
        check=True,
        **_subprocess_kwargs(),
    )
    return wav_path, ".wav", wav_path


# ── Whisper (lazy singleton) ─────────────────────────────────────────────────
_whisper_model = None
_whisper_lock = threading.Lock()


def _missing_cuda_runtime(exc):
    text = str(exc).lower()
    return any(token in text for token in ("cublas", "cudnn", "cuda", "cufft", "curand"))


def _iter_windows_cuda_dirs():
    if os.name != "nt":
        return []
    dirs = []
    seen = set()
    for path in sys.path:
        if not path:
            continue
        site_packages = os.path.abspath(path)
        if not os.path.isdir(site_packages):
            continue
        if os.path.basename(site_packages).lower() != "site-packages":
            continue
        for rel in (
            os.path.join("nvidia", "cublas", "bin"),
            os.path.join("nvidia", "cuda_runtime", "bin"),
            os.path.join("nvidia", "cuda_nvrtc", "bin"),
            "ctranslate2",
        ):
            dll_dir = os.path.join(site_packages, rel)
            if os.path.isdir(dll_dir) and dll_dir not in seen:
                seen.add(dll_dir)
                dirs.append(dll_dir)
    return dirs


def _prepare_windows_cuda_runtime():
    global _CUDA_RUNTIME_PREPPED
    if os.name != "nt" or _CUDA_RUNTIME_PREPPED:
        return

    dll_dirs = _iter_windows_cuda_dirs()
    if dll_dirs:
        current_path = os.environ.get("PATH", "")
        current_parts = current_path.split(os.pathsep) if current_path else []
        prepend = [dll_dir for dll_dir in dll_dirs if dll_dir not in current_parts]
        if prepend:
            os.environ["PATH"] = os.pathsep.join(prepend + [current_path]) if current_path else os.pathsep.join(prepend)
        for dll_dir in dll_dirs:
            try:
                os.add_dll_directory(dll_dir)
            except (AttributeError, FileNotFoundError, OSError):
                pass

    _CUDA_RUNTIME_PREPPED = True


def _whisper_candidates(prefer_cpu=False):
    cfg = _load_config()
    model_name = cfg.get("whisper_model", "large-v3")
    force_device = cfg.get("whisper_device", "auto")
    candidates = []

    allow_cuda = not prefer_cpu and force_device in ("auto", "cuda")
    if allow_cuda:
        _prepare_windows_cuda_runtime()
        candidates.append((model_name, "cuda", "float16"))
    if force_device in ("auto", "cpu") or prefer_cpu:
        candidates.append((model_name, "cpu", "int8"))
    fast_model = WHISPER_QUALITY_MODELS["fast"]
    if model_name != fast_model:
        candidates.append((fast_model, "cpu", "int8"))
    return candidates


def _load_whisper_model(prefer_cpu=False):
    from faster_whisper import WhisperModel
    last_error = None
    for model_name, device, compute_type in _whisper_candidates(prefer_cpu=prefer_cpu):
        try:
            return WhisperModel(
                model_name,
                device=device,
                compute_type=compute_type,
            )
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Unable to load Whisper model: {last_error}")


def _get_whisper(prefer_cpu=False, reset=False):
    global _whisper_model
    with _whisper_lock:
        if reset:
            _whisper_model = None
        if _whisper_model is None:
            _whisper_model = _load_whisper_model(prefer_cpu=prefer_cpu)
    return _whisper_model


def _transcribe_segments(audio_file_path, **kwargs):
    try:
        model = _get_whisper()
        return model.transcribe(audio_file_path, **kwargs)
    except Exception as exc:
        if not _missing_cuda_runtime(exc):
            raise
        model = _get_whisper(prefer_cpu=True, reset=True)
        return model.transcribe(audio_file_path, **kwargs)


def _transcription_backend(cfg=None):
    cfg = cfg or _load_config()
    return str(cfg.get("transcription_backend", "local")).lower()


def _selected_openai_transcription_model(cfg=None):
    cfg = cfg or _load_config()
    model_name = str(cfg.get("openai_transcription_model", OPENAI_TRANSCRIPTION_DEFAULT_MODEL)).strip()
    return model_name if model_name in OPENAI_TRANSCRIPTION_MODELS else OPENAI_TRANSCRIPTION_DEFAULT_MODEL


def _openai_transcription_api_key(cfg=None):
    cfg = cfg or _load_config()
    return str(cfg.get("openai_api_key") or os.environ.get("OPENAI_API_KEY") or "").strip()


def _encode_multipart_form(fields, file_field_name, file_path):
    boundary = f"----MuesliBoundary{uuid.uuid4().hex}"
    body = bytearray()

    def _append_text(name, value):
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")

    for field_name, field_value in fields.items():
        if field_value is None:
            continue
        _append_text(field_name, field_value)

    filename = os.path.basename(file_path)
    ext = os.path.splitext(filename)[1].lower()
    content_type = {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".mp4": "audio/mp4",
        ".mpga": "audio/mpeg",
        ".mpeg": "audio/mpeg",
        ".webm": "audio/webm",
    }.get(ext, "application/octet-stream")
    with open(file_path, "rb") as f:
        file_bytes = f.read()

    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(
        f'Content-Disposition: form-data; name="{file_field_name}"; filename="{filename}"\r\n'.encode("utf-8")
    )
    body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
    body.extend(file_bytes)
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    return boundary, bytes(body)


def _transcribe_openai(audio_file_path):
    cfg = _load_config()
    api_key = _openai_transcription_api_key(cfg)
    if not api_key:
        raise RuntimeError("OpenAI transcription is selected but no OPENAI_API_KEY or saved OpenAI key is configured")

    model_name = _selected_openai_transcription_model(cfg)
    boundary, body = _encode_multipart_form(
        {
            "model": model_name,
            "response_format": "json",
        },
        "file",
        audio_file_path,
    )
    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/transcriptions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=OPENAI_TRANSCRIPTION_TIMEOUT_SEC) as resp:
            payload = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI transcription failed ({exc.code}): {error_body[:240]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenAI transcription failed: {exc.reason}") from exc

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError("OpenAI transcription returned invalid JSON") from exc

    transcript = str(data.get("text", "")).strip()
    if not transcript:
        raise RuntimeError("OpenAI transcription returned no text")
    return transcript


# ── LLM (lazy singleton) ─────────────────────────────────────────────────────
_llm_model = None
_llm_lock = threading.Lock()
_LLM_MODEL_DIR = os.path.join(APP_DIR, "models")
_OLLAMA_BASE_URL = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")


def _list_gguf_models():
    if not os.path.isdir(_LLM_MODEL_DIR):
        return []
    return [f for f in sorted(os.listdir(_LLM_MODEL_DIR)) if f.endswith(".gguf")]


def _selected_gguf_model_path():
    cfg = _load_config()
    selected = cfg.get("llm_local_model", "")
    models = _list_gguf_models()
    if selected and selected in models:
        return os.path.join(_LLM_MODEL_DIR, selected)
    if models:
        return os.path.join(_LLM_MODEL_DIR, models[0])
    return None


def _ollama_request(path, payload=None, timeout=3):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{_OLLAMA_BASE_URL}{path}", data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body) if body else {}


def _list_ollama_models():
    try:
        payload = _ollama_request("/api/tags")
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return []
    models = [m.get("name", "").strip() for m in payload.get("models", [])]
    return [m for m in models if m]


def _recommend_ollama_model(models):
    if not models:
        return None
    lowered = {name.lower(): name for name in models}
    for candidate in (
        "deepseek-r1:8b",
        "qwen3:8b",
        "qwen3.5:4b",
        "qwen3.5:9b",
        "gemma4:e4b",
        "gemma4:4b",
        "llama3.1:8b",
        "mistral:7b",
        "phi4",
    ):
        if candidate in lowered:
            return lowered[candidate]
    filtered = [
        name for name in models
        if not any(token in name.lower() for token in ("coder", "code", "vl", "vision", "embed", "cloud"))
    ]
    return filtered[0] if filtered else models[0]


def _selected_ollama_model():
    cfg = _load_config()
    selected = str(cfg.get("ollama_model", "")).strip()
    models = _list_ollama_models()
    if selected and selected in models:
        return selected
    return _recommend_ollama_model(models)


def _llm_backend():
    cfg = _load_config()
    return str(cfg.get("llm_backend", "auto")).lower()


def _get_llm():
    global _llm_model
    if _llm_model is None:
        with _llm_lock:
            if _llm_model is None:
                from llama_cpp import Llama
                model_path = _selected_gguf_model_path()
                if not model_path:
                    raise FileNotFoundError(f"No .gguf model in {_LLM_MODEL_DIR}")
                _llm_model = Llama(
                    model_path=model_path, n_ctx=4096, n_threads=4, verbose=False
                )
    return _llm_model


def _llm_generate(prompt_text):
    """Run prompt through the configured LLM backend."""
    cfg = _load_config()
    backend = _llm_backend()
    api_key = cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    anthropic_allowed = backend in ("auto", "anthropic")
    ollama_allowed = backend in ("auto", "ollama")
    local_allowed = backend in ("auto", "local")

    if anthropic_allowed and api_key:
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

    if backend == "anthropic" and not api_key:
        raise RuntimeError("Anthropic is selected but no API key is configured")

    if ollama_allowed:
        try:
            model_name = _selected_ollama_model()
            if not model_name:
                raise RuntimeError("No Ollama model is installed")
            resp = _ollama_request(
                "/api/generate",
                {"model": model_name, "prompt": prompt_text, "stream": False},
                timeout=420,
            )
            text = str(resp.get("response", "")).strip()
            if text:
                return text
        except Exception:
            pass

    if backend == "ollama":
        raise RuntimeError("Ollama is selected but no usable Ollama model is available")

    if local_allowed:
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

    if backend == "local":
        raise RuntimeError("Local GGUF is selected but no local model is available")

    raise RuntimeError("No LLM available for the configured backend")


# ── Chunk pipeline ───────────────────────────────────────────────────────────
class _ChunkPipeline:
    """Background worker: transcribe + summarise short live audio chunks."""

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
        while True:
            path = self._queue.get()
            if path is None:
                self._done.set()
                return
            # Transcribe
            try:
                segments, _ = _transcribe_segments(path, beam_size=5)
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

            # Keep live transcript delivery independent from slower LLM work.
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
        self._sd_chunk_frames = []
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
                            each time a live chunk is transcribed.
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
        self._sd_chunk_frames = []
        selected_device = _resolve_audio_input_device(self._record_backend)

        self._pipeline = _ChunkPipeline(on_transcribed=on_transcribed)
        if self._record_backend == "pyaudio":
            import pyaudio
            open_kwargs = dict(
                format=pyaudio.paInt16,
                channels=CHANNELS,
                rate=RATE,
                input=True,
                frames_per_buffer=CHUNK,
            )
            if selected_device is not None:
                open_kwargs["input_device_index"] = selected_device
            try:
                self._stream = self._pa.open(**open_kwargs)
            except Exception:
                open_kwargs.pop("input_device_index", None)
                self._stream = self._pa.open(**open_kwargs)
            self._rec_thread = threading.Thread(target=self._record_loop, daemon=True)
            self._rec_thread.start()
        else:
            stream_kwargs = dict(
                samplerate=RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK,
                callback=self._record_sounddevice,
            )
            if selected_device is not None:
                stream_kwargs["device"] = selected_device
            try:
                self._stream = sd.InputStream(**stream_kwargs)
            except Exception:
                stream_kwargs.pop("device", None)
                self._stream = sd.InputStream(**stream_kwargs)
            self._stream.start()

    def _record_sounddevice(self, indata, frames, time_info, status):
        if not self._recording:
            return
        data = indata.copy().tobytes()
        self._frames.append(data)
        self._sd_chunk_frames.append(data)
        if len(self._sd_chunk_frames) >= _READS_PER_CHUNK:
            self._emit_chunk(self._sd_chunk_frames)
            self._sd_chunk_frames = []

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
        self._chunk_idx += 1
        path = os.path.join(REC_DIR, f"{self._rec_slug}_chunk{self._chunk_idx}.wav")
        wf = wave.open(path, "wb")
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(self._sample_width)
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
        if self._record_backend == "sounddevice" and self._sd_chunk_frames:
            self._emit_chunk(self._sd_chunk_frames)
            self._sd_chunk_frames = []

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

        started_at = datetime.datetime.now().isoformat()
        meta = {
            "slug": slug,
            "title": _default_session_title(started_at),
            "started_at": started_at,
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
        self._sd_chunk_frames = []

        return meta

    def _finalize(self, meta, wav_path, transcript, summaries):
        """Convert audio, run final summary, save everything. Returns updated meta."""
        slug = meta["slug"]
        mp3_path = os.path.join(self._shared_dir, slug + ".mp3")
        wav_dest = os.path.join(self._shared_dir, slug + ".wav")
        active_summary_mode = _get_summary_mode()

        # WAV -> MP3
        if not os.path.exists(mp3_path) and not os.path.exists(wav_dest):
            if _ffmpeg_available():
                try:
                    subprocess.run(
                        ["ffmpeg", "-y", "-i", wav_path,
                         "-ac", "1", "-ar", "16000", "-b:a", "64k", mp3_path],
                        capture_output=True, check=True,
                        **_subprocess_kwargs(),
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
        llm_used = False
        try:
            if summaries and any(summaries):
                merged = "\n".join(f"- {s}" for s in summaries if s)
                prompt_text = active_summary_mode["prompt"].replace(
                    "{transcript}",
                    f"[Chunk summaries from a longer recording]\n{merged}",
                )
            else:
                prompt_text = active_summary_mode["prompt"].replace("{transcript}", transcript or "")
            raw = _llm_generate(prompt_text)
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
            ai = json.loads(raw)
            llm_used = True
        except Exception:
            ai = _fallback_ai_fields(transcript)

        default_title = _default_session_title(meta.get("started_at", ""))
        title = ai.get("title", "").strip() or default_title
        summary = ai.get("summary", "").strip() if llm_used else ""
        title, summary = _apply_manual_note_overrides(meta, title, summary)
        speakers = int(ai.get("speakers", 0) or 0) if llm_used else 0
        corrections = ai.get("corrections", "")
        bugs = ai.get("bugs", "")

        # Only rename files when an LLM supplied a real title.
        new_slug = slug
        if (llm_used or meta.get("title_manual")) and title:
            base_slug = _slug_from_title(title)
            candidate = base_slug
            i = 2
            while (os.path.exists(os.path.join(REC_DIR, candidate + ".json"))
                   and candidate != slug):
                candidate = f"{base_slug}-{i}"
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
        txt_body = f"# {title}\n\n"
        if summary:
            txt_body += f"## Summary\n\n{summary}\n\n"
        if corrections:
            txt_body += f"## Transcription Corrections\n\n{corrections}\n\n"
        if bugs:
            txt_body += f"## Bugs / Issues Mentioned\n\n{bugs}\n\n"
        txt_body += f"## Transcript\n\n{transcript}\n"
        with open(txt_path, "w", encoding="utf-8") as f:
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
            "summary_mode": active_summary_mode["id"],
            "summary_mode_title": active_summary_mode["title"],
        })
        meta.pop("error", None)
        _save_meta(meta)
        return meta

    # ── Process existing file ────────────────────────────────────────────────

    def process_file(self, audio_file_path):
        """Transcribe and summarise an existing audio file.

        Returns the full session dict.
        """
        if not os.path.exists(audio_file_path):
            raise FileNotFoundError(audio_file_path)

        slug = _datetime_slug()
        import_path, _, wav_path = _normalise_import_audio(audio_file_path, slug, self._shared_dir)

        # Get duration via ffprobe if available
        duration = self._probe_duration(audio_file_path)

        started_at = datetime.datetime.now().isoformat()
        meta = {
            "slug": slug,
            "title": _default_session_title(started_at),
            "started_at": started_at,
            "duration": duration,
            "status": "processing",
            "summary": "",
            "transcript": "",
            "speakers": 0,
        }
        _save_meta(meta)

        # Transcribe
        transcript = self.transcribe(import_path)

        # Finalise (summary, rename, save)
        meta = self._finalize(
            meta,
            wav_path or import_path,
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
                **_subprocess_kwargs(),
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
        if _transcription_backend() in ("openai", "openai_realtime"):
            if on_percent:
                on_percent(5)
            transcript = _transcribe_openai(audio_file_path)
            if on_percent:
                on_percent(100)
            return transcript
        segments, info = _transcribe_segments(audio_file_path, beam_size=5)
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

    def summarize(self, transcript, prompt_text=None):
        """Summarise a transcript string. Returns dict with title, summary, speakers, etc."""
        prompt_text = (prompt_text or _get_summary_prompt()).replace("{transcript}", transcript)
        try:
            raw = _llm_generate(prompt_text)
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
            return json.loads(raw)
        except Exception:
            return _fallback_ai_fields(transcript)

    def resummarize_session(self, session_or_slug, prompt_text=None, mode_id=None, mode_title=None):
        """Re-run summary/title generation for an existing session."""
        meta = dict(session_or_slug) if isinstance(session_or_slug, dict) else (self.get_session(session_or_slug) or {})
        slug = meta.get("slug", "")
        if not slug:
            raise ValueError("Missing session slug")

        transcript = (meta.get("transcript") or "").strip()
        if not transcript:
            audio_path = self.audio_path(slug) or self._find_audio_for_slug(slug)
            if not audio_path:
                raise FileNotFoundError(f"No audio found for session {slug}")
            transcript = self.transcribe(audio_path)

        mode = _get_summary_mode(mode_id)
        ai = self.summarize(transcript, prompt_text=prompt_text or mode["prompt"])
        llm_used = bool(
            (ai.get("title") or "").strip()
            or (ai.get("summary") or "").strip()
            or int(ai.get("speakers", 0) or 0)
            or (ai.get("corrections") or "").strip()
            or (ai.get("bugs") or "").strip()
        )

        default_title = _default_session_title(meta.get("started_at", ""))
        title = (ai.get("title") or "").strip() or default_title
        summary = (ai.get("summary") or "").strip() if llm_used else ""
        title, summary = _apply_manual_note_overrides(meta, title, summary)
        speakers = int(ai.get("speakers", 0) or 0) if llm_used else 0
        corrections = ai.get("corrections", "")
        bugs = ai.get("bugs", "")

        new_slug = slug
        if (llm_used or meta.get("title_manual")) and title:
            base_slug = _slug_from_title(title)
            candidate = base_slug
            i = 2
            while (os.path.exists(os.path.join(REC_DIR, candidate + ".json")) and candidate != slug):
                candidate = f"{base_slug}-{i}"
                i += 1
            new_slug = candidate

        if new_slug != slug:
            for ext in (".mp3", ".wav"):
                old_audio = os.path.join(self._shared_dir, slug + ext)
                new_audio = os.path.join(self._shared_dir, new_slug + ext)
                if os.path.exists(old_audio):
                    os.rename(old_audio, new_audio)
                    break

        txt_path = os.path.join(self._shared_dir, new_slug + ".txt")
        txt_body = f"# {title}\n\n"
        if summary:
            txt_body += f"## Summary\n\n{summary}\n\n"
        if corrections:
            txt_body += f"## Transcription Corrections\n\n{corrections}\n\n"
        if bugs:
            txt_body += f"## Bugs / Issues Mentioned\n\n{bugs}\n\n"
        txt_body += f"## Transcript\n\n{transcript}\n"
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(txt_body)

        old_txt = os.path.join(self._shared_dir, slug + ".txt")
        if new_slug != slug and os.path.exists(old_txt):
            os.remove(old_txt)

        old_json = os.path.join(REC_DIR, slug + ".json")
        if new_slug != slug and os.path.exists(old_json):
            os.remove(old_json)

        meta.update({
            "slug": new_slug,
            "title": title,
            "summary": summary,
            "transcript": transcript,
            "speakers": speakers,
            "corrections": corrections,
            "bugs": bugs,
            "status": "done",
            "audio_path": self.audio_path(new_slug),
            "summary_mode": mode_id or mode["id"],
            "summary_mode_title": mode_title or mode["title"],
        })
        meta.pop("error", None)
        _save_meta(meta)
        return meta

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
