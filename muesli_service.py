#!/usr/bin/env python3
"""Local HTTP service for Muesli note and transcription workflows."""

import argparse
import datetime
import json
import mimetypes
import os
import threading
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import muesli
from muesli import Muesli
from muesli_runtime import is_processing_paused, set_processing_paused

DEFAULT_PORT = 8765


def _utc_now():
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _safe_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_bool(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _normalize_component_name(component):
    component = str(component or "").strip().lower()
    aliases = {
        "date": "started_at",
        "audio_path": "audio",
    }
    return aliases.get(component, component)


class JobStore:
    def __init__(self):
        self._jobs = {}
        self._lock = threading.Lock()

    def _snapshot(self, job, include_result=False):
        data = {k: v for k, v in job.items() if include_result or k != "result"}
        if not include_result and "result" in job:
            result = job["result"]
            if isinstance(result, dict):
                preview = dict(result)
                transcript = preview.get("transcript")
                if isinstance(transcript, str) and len(transcript) > 240:
                    preview["transcript"] = transcript[:237].rstrip() + "..."
                data["result_preview"] = preview
            else:
                data["result_preview"] = result
        return data

    def create(self, kind, payload, fn):
        job_id = uuid.uuid4().hex
        job = {
            "id": job_id,
            "kind": kind,
            "payload": payload,
            "status": "queued",
            "created_at": _utc_now(),
            "started_at": None,
            "finished_at": None,
            "result": None,
            "error": None,
        }
        with self._lock:
            self._jobs[job_id] = job
        worker = threading.Thread(target=self._run, args=(job_id, fn), daemon=True)
        worker.start()
        return self._snapshot(job, include_result=True)

    def _run(self, job_id, fn):
        with self._lock:
            job = self._jobs[job_id]
            job["status"] = "running"
            job["started_at"] = _utc_now()
        try:
            result = fn()
        except Exception as exc:
            result = None
            error = f"{type(exc).__name__}: {exc}"
            trace = traceback.format_exc(limit=8)
            with self._lock:
                job = self._jobs[job_id]
                job["status"] = "error"
                job["finished_at"] = _utc_now()
                job["error"] = error
                job["traceback"] = trace
            return
        with self._lock:
            job = self._jobs[job_id]
            job["status"] = "done"
            job["finished_at"] = _utc_now()
            job["result"] = result

    def list(self):
        with self._lock:
            jobs = [self._snapshot(job) for job in self._jobs.values()]
        jobs.sort(key=lambda job: job.get("created_at", ""), reverse=True)
        return jobs

    def get(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            return self._snapshot(job, include_result=True)


class MuesliHttpService:
    def __init__(self, app=None):
        self.app = app or Muesli()
        self.jobs = JobStore()
        self._record_lock = threading.Lock()

    def capabilities(self):
        cfg = muesli._load_config()
        summary_modes = [
            {
                "id": mode.get("id"),
                "title": mode.get("title"),
            }
            for mode in cfg.get("summary_modes", [])
        ]
        return {
            "service": "muesli-service",
            "recording": {
                "start_stop": True,
                "default_microphone": True,
            },
            "transcription_backends": [
                "local",
                "openai",
                "openai_realtime",
            ],
            "summary_modes": summary_modes,
            "supports_jobs": [
                "transcribe-file",
                "ingest-voice-note",
                "reprocess-note",
                "stop-recording",
            ],
            "components": [
                "audio",
                "transcript",
                "summary",
                "title",
                "started_at",
                "duration",
                "speakers",
                "status",
                "corrections",
                "bugs",
            ],
        }

    def recording_status(self):
        started_at = None
        if getattr(self.app, "_rec_started", None):
            try:
                started_at = self.app._rec_started.isoformat()
            except Exception:
                started_at = str(self.app._rec_started)
        return {
            "recording": bool(getattr(self.app, "_recording", False)),
            "slug": getattr(self.app, "_rec_slug", None),
            "started_at": started_at,
        }

    def start_recording(self):
        with self._record_lock:
            if getattr(self.app, "_recording", False):
                raise RuntimeError("A recording is already in progress.")
            self.app.start_recording()
            return self.recording_status()

    def stop_recording(self):
        with self._record_lock:
            if not getattr(self.app, "_recording", False):
                raise RuntimeError("No recording is in progress.")
            return self.jobs.create(
                "stop-recording",
                {},
                lambda: self.app.stop_recording(),
            )

    def list_notes(self, limit=50, status=None):
        notes = self.app.list_sessions()
        if status:
            notes = [note for note in notes if str(note.get("status", "")).lower() == str(status).lower()]
        return notes[: max(1, limit)]

    def latest_note(self):
        return self.app.get_latest_session()

    def get_note(self, slug):
        return self.app.get_session(slug)

    def delete_note(self, slug):
        return self.app.delete_session(slug)

    def search_notes(self, query, component="any", limit=20):
        return self.app.search_sessions(query, component=component, limit=limit)

    def overwrite_component(self, slug, component, value):
        component = _normalize_component_name(component)
        allowed = {"title", "summary", "transcript", "started_at", "status", "duration", "speakers", "corrections", "bugs"}
        if component not in allowed:
            raise ValueError(f"Unsupported writable component: {component}")
        return self.app.overwrite_session(slug, **{component: value})

    def submit_transcribe_file(self, path):
        payload = {"path": path}
        return self.jobs.create(
            "transcribe-file",
            payload,
            lambda: {
                "path": path,
                "transcript": self.app.transcribe(path),
            },
        )

    def submit_ingest_voice_note(self, path):
        payload = {"path": path}
        return self.jobs.create(
            "ingest-voice-note",
            payload,
            lambda: self.app.process_file(path),
        )

    def submit_reprocess_note(self, slug, force_transcribe=True, prompt_text=None, mode_id=None, mode_title=None):
        payload = {
            "slug": slug,
            "force_transcribe": bool(force_transcribe),
            "mode_id": mode_id,
            "mode_title": mode_title,
        }
        return self.jobs.create(
            "reprocess-note",
            payload,
            lambda: self._reprocess_note(
                slug,
                force_transcribe=force_transcribe,
                prompt_text=prompt_text,
                mode_id=mode_id,
                mode_title=mode_title,
            ),
        )

    def _reprocess_note(self, slug, force_transcribe=True, prompt_text=None, mode_id=None, mode_title=None):
        meta = self.app.get_session(slug)
        if not meta:
            raise FileNotFoundError(f"No session found for slug {slug}")
        if force_transcribe:
            meta["transcript"] = ""
        meta["status"] = "processing"
        meta["processing_stage"] = "Reprocessing from saved audio"
        muesli._save_meta(meta)
        result = self.app.resummarize_session(
            meta,
            prompt_text=prompt_text,
            mode_id=mode_id,
            mode_title=mode_title,
        )
        result.pop("processing_stage", None)
        muesli._save_meta(result)
        return result


def _component_payload(meta, component):
    component = _normalize_component_name(component)
    slug = meta.get("slug", "")
    if component == "audio":
        audio_path = meta.get("audio_path")
        if not audio_path or not os.path.exists(audio_path):
            raise FileNotFoundError(f"No audio found for session {slug}")
        mime_type = mimetypes.guess_type(audio_path)[0] or "application/octet-stream"
        return {
            "component": "audio",
            "slug": slug,
            "path": audio_path,
            "exists": True,
            "mime_type": mime_type,
            "size_bytes": os.path.getsize(audio_path),
            "length": float(meta.get("duration") or 0),
        }

    field_map = {
        "title": meta.get("title", ""),
        "summary": meta.get("summary", ""),
        "transcript": meta.get("transcript", ""),
        "started_at": meta.get("started_at", ""),
        "duration": float(meta.get("duration") or 0),
        "speakers": int(meta.get("speakers", 0) or 0),
        "status": meta.get("status", ""),
        "corrections": meta.get("corrections", ""),
        "bugs": meta.get("bugs", ""),
    }
    if component not in field_map:
        raise ValueError(f"Unsupported component: {component}")
    value = field_map[component]
    if isinstance(value, str):
        size_bytes = len(value.encode("utf-8"))
        length = len(value)
    else:
        size_bytes = None
        length = value
    return {
        "component": component,
        "slug": slug,
        "value": value,
        "size_bytes": size_bytes,
        "length": length,
    }


def _openapi_spec(port):
    base_url = f"http://localhost:{port}"
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Muesli Service",
            "version": "1.0.0",
            "description": "Local voice-note service for recording audio, processing notes, and retrieving transcripts or raw audio.",
        },
        "servers": [{"url": base_url}],
        "paths": {
            "/health": {"get": {"summary": "Cheap health check for the local service."}},
            "/capabilities": {"get": {"summary": "List supported components, job types, and summary modes."}},
            "/notes": {"get": {"summary": "List saved voice notes, newest first."}},
            "/notes/latest": {"get": {"summary": "Get the newest saved voice note."}},
            "/notes/{slug}": {
                "get": {"summary": "Get one saved voice note by slug."},
                "delete": {"summary": "Delete a saved voice note and its managed artifacts."},
            },
            "/notes/{slug}/status": {"get": {"summary": "Get the current processing status for one note."}},
            "/notes/{slug}/components/{component}": {
                "get": {"summary": "Get one note component such as audio, transcript, summary, or title."},
                "put": {"summary": "Overwrite a writable component such as title, summary, or transcript."},
            },
            "/notes/search": {"post": {"summary": "Deterministically search saved notes by substring match."}},
            "/recordings/status": {"get": {"summary": "Get the live default-mic recording state."}},
            "/recordings/start": {"post": {"summary": "Start recording from the default microphone."}},
            "/recordings/stop": {"post": {"summary": "Stop the active recording and finalize it as an async job."}},
            "/jobs": {"get": {"summary": "List recent async jobs."}},
            "/jobs/{jobId}": {"get": {"summary": "Get one async job with status and result."}},
            "/jobs/transcribe-file": {"post": {"summary": "Transcribe an audio file asynchronously and return transcript text."}},
            "/jobs/ingest-voice-note": {"post": {"summary": "Add an audio file as a managed voice note and process it locally."}},
            "/jobs/reprocess-note": {"post": {"summary": "Reprocess a saved note, optionally forcing fresh transcription from audio."}},
            "/processing/status": {"get": {"summary": "Get the current pause flag for local Whisper + summary work."}},
            "/processing/pause": {"post": {"summary": "Pause local Whisper + summary work. Aliases agents may use: 'stop the GPU work', 'silence this machine', 'pause processing'. Queued jobs stay queued and resume automatically when /processing/resume is called."}},
            "/processing/resume": {"post": {"summary": "Resume local Whisper + summary work. One resume call drains every queued job (chunk pipeline, in-flight transcribe, scheduled reprocess)."}},
        },
    }


def _make_handler(service, port):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MuesliService/1.0"

        def log_message(self, fmt, *args):
            return

        def _send_json(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_binary(self, status, body, mime_type, download_name=None):
            self.send_response(status)
            self.send_header("Content-Type", mime_type)
            self.send_header("Content-Length", str(len(body)))
            if download_name:
                self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self):
            length = _safe_int(self.headers.get("Content-Length"), 0)
            raw = self.rfile.read(length) if length > 0 else b""
            if not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception as exc:
                raise ValueError(f"Invalid JSON body: {exc}")

        def _split(self):
            parsed = urlsplit(self.path)
            segments = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
            query = parse_qs(parsed.query)
            return segments, query

        def _require_note(self, slug):
            note = service.get_note(slug)
            if not note:
                self._send_json(404, {"error": f"No session found for slug {slug}"})
                return None
            return note

        def do_GET(self):
            try:
                segments, query = self._split()
                if not segments:
                    return self._send_json(200, {"service": "muesli-service", "openapi": f"http://localhost:{port}/openapi.json"})
                if segments == ["health"]:
                    return self._send_json(200, {"ok": True, "service": "muesli-service", "time": _utc_now()})
                if segments == ["openapi.json"]:
                    return self._send_json(200, _openapi_spec(port))
                if segments == ["capabilities"]:
                    return self._send_json(200, service.capabilities())
                if segments == ["notes"]:
                    limit = _safe_int((query.get("limit") or ["50"])[0], 50)
                    status = (query.get("status") or [None])[0]
                    return self._send_json(200, {"notes": service.list_notes(limit=limit, status=status)})
                if segments == ["notes", "latest"]:
                    note = service.latest_note()
                    if not note:
                        return self._send_json(404, {"error": "No saved notes found"})
                    return self._send_json(200, note)
                if len(segments) == 2 and segments[0] == "notes":
                    note = self._require_note(segments[1])
                    if note is not None:
                        return self._send_json(200, note)
                    return
                if len(segments) == 3 and segments[0] == "notes" and segments[2] == "status":
                    note = self._require_note(segments[1])
                    if note is not None:
                        return self._send_json(200, {
                            "slug": note.get("slug"),
                            "status": note.get("status"),
                            "processing_stage": note.get("processing_stage", ""),
                        })
                    return
                if len(segments) == 4 and segments[0] == "notes" and segments[2] == "components":
                    note = self._require_note(segments[1])
                    if note is None:
                        return
                    payload = _component_payload(note, segments[3])
                    if segments[3].lower() == "audio" and _safe_bool((query.get("download") or ["0"])[0]):
                        with open(payload["path"], "rb") as f:
                            data = f.read()
                        return self._send_binary(200, data, payload["mime_type"], os.path.basename(payload["path"]))
                    return self._send_json(200, payload)
                if segments == ["recordings", "status"]:
                    return self._send_json(200, service.recording_status())
                if segments == ["processing", "status"]:
                    return self._send_json(200, {"paused": is_processing_paused()})
                if segments == ["jobs"]:
                    return self._send_json(200, {"jobs": service.jobs.list()})
                if len(segments) == 2 and segments[0] == "jobs":
                    job = service.jobs.get(segments[1])
                    if not job:
                        return self._send_json(404, {"error": f"No job found for id {segments[1]}"})
                    return self._send_json(200, job)
                return self._send_json(404, {"error": "Unknown endpoint"})
            except FileNotFoundError as exc:
                return self._send_json(404, {"error": str(exc)})
            except ValueError as exc:
                return self._send_json(400, {"error": str(exc)})
            except Exception as exc:
                return self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})

        def do_POST(self):
            try:
                segments, _query = self._split()
                body = self._read_json_body()
                if segments == ["notes", "search"]:
                    query = body.get("query", "")
                    component = body.get("component", "any")
                    limit = _safe_int(body.get("limit"), 20)
                    return self._send_json(200, {
                        "results": service.search_notes(query, component=component, limit=limit)
                    })
                if segments == ["recordings", "start"]:
                    return self._send_json(202, service.start_recording())
                if segments == ["recordings", "stop"]:
                    return self._send_json(202, service.stop_recording())
                if segments == ["processing", "pause"]:
                    set_processing_paused(True)
                    return self._send_json(200, {"paused": True})
                if segments == ["processing", "resume"]:
                    set_processing_paused(False)
                    return self._send_json(200, {"paused": False})
                if segments == ["jobs", "transcribe-file"]:
                    path = str(body.get("path") or "").strip()
                    if not path or not os.path.exists(path):
                        raise FileNotFoundError(path or "Missing audio path")
                    return self._send_json(202, service.submit_transcribe_file(path))
                if segments == ["jobs", "ingest-voice-note"]:
                    path = str(body.get("path") or "").strip()
                    if not path or not os.path.exists(path):
                        raise FileNotFoundError(path or "Missing audio path")
                    return self._send_json(202, service.submit_ingest_voice_note(path))
                if segments == ["jobs", "reprocess-note"]:
                    slug = str(body.get("slug") or "").strip()
                    if not slug:
                        raise ValueError("Missing slug")
                    force_transcribe = body.get("force_transcribe", True)
                    if isinstance(force_transcribe, str):
                        force_transcribe = _safe_bool(force_transcribe)
                    return self._send_json(202, service.submit_reprocess_note(
                        slug,
                        force_transcribe=force_transcribe,
                        prompt_text=body.get("prompt_text"),
                        mode_id=body.get("mode_id"),
                        mode_title=body.get("mode_title"),
                    ))
                return self._send_json(404, {"error": "Unknown endpoint"})
            except FileNotFoundError as exc:
                return self._send_json(404, {"error": str(exc)})
            except ValueError as exc:
                return self._send_json(400, {"error": str(exc)})
            except Exception as exc:
                return self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})

        def do_PUT(self):
            try:
                segments, _query = self._split()
                body = self._read_json_body()
                if len(segments) == 4 and segments[0] == "notes" and segments[2] == "components":
                    value = body.get("value")
                    note = service.overwrite_component(segments[1], segments[3], value)
                    return self._send_json(200, note)
                return self._send_json(404, {"error": "Unknown endpoint"})
            except FileNotFoundError as exc:
                return self._send_json(404, {"error": str(exc)})
            except ValueError as exc:
                return self._send_json(400, {"error": str(exc)})
            except Exception as exc:
                return self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})

        def do_DELETE(self):
            try:
                segments, _query = self._split()
                if len(segments) == 2 and segments[0] == "notes":
                    deleted = service.delete_note(segments[1])
                    if not deleted:
                        return self._send_json(404, {"error": f"No session found for slug {segments[1]}"})
                    return self._send_json(200, {"deleted": True, "slug": segments[1]})
                return self._send_json(404, {"error": "Unknown endpoint"})
            except Exception as exc:
                return self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})

    return Handler


def serve(port=DEFAULT_PORT, host="127.0.0.1", service=None):
    service = service or MuesliHttpService()
    server = ThreadingHTTPServer((host, port), _make_handler(service, port))
    return server


def main():
    parser = argparse.ArgumentParser(description="Run the local Muesli HTTP service.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    server = serve(port=args.port, host=args.host)
    print(f"Muesli service listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
