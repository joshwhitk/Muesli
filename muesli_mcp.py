#!/usr/bin/env python3
"""MCP transport for Muesli.

Thin stdio MCP server that wraps the existing localhost HTTP service
(muesli_service.py, default port 8765). All tool calls are proxied as
HTTP requests, so HTTP and MCP share one JobStore and one recording state
machine — pause/resume, in-flight jobs, and recording start/stop are
consistent across transports.

Run:
    python muesli_mcp.py
    # or, point the host port at something else:
    MUESLI_SERVICE_URL=http://localhost:9000 python muesli_mcp.py

Prereq:
    muesli_service.py must already be running. If it is not, every tool
    call returns a clear error directing the caller to start it. We do
    not auto-spawn the service here — that is a deliberate follow-up so
    the MCP server stays a pure transport with no process-lifecycle
    concerns.

Tools mirror the HTTP routes documented in muesli_service.py's openapi
spec; the names match MUESLI_API.md's "Likely MCP / Local Service
Mapping" section so the registry surface and the docs stay aligned.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "muesli_mcp.py requires the `mcp` package. Install with:\n"
        "    .venv\\Scripts\\pip install mcp\n"
        f"(import error: {exc})"
    )


SERVICE_URL = os.environ.get("MUESLI_SERVICE_URL", "http://localhost:8765").rstrip("/")
HTTP_TIMEOUT_SEC = float(os.environ.get("MUESLI_MCP_HTTP_TIMEOUT", "60"))


class ServiceUnavailable(RuntimeError):
    pass


def _request(method: str, path: str, body: dict | None = None) -> Any:
    url = f"{SERVICE_URL}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = Request(url, data=data, method=method, headers=headers)
    try:
        with urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            raw = resp.read()
    except URLError as exc:
        # Connection refused, DNS, etc. — service almost certainly not running.
        raise ServiceUnavailable(
            f"Could not reach muesli-service at {SERVICE_URL} ({exc.reason}). "
            f"Start it with: python muesli_service.py"
        ) from exc
    except HTTPError as exc:
        # Service is up but returned 4xx/5xx — surface its body.
        body_text = ""
        try:
            body_text = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {exc.code} from {url}: {body_text or exc.reason}") from exc
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return raw.decode("utf-8", errors="replace")


# ── Tool implementations ─────────────────────────────────────────────────────

def _tool_list_notes(args: dict) -> Any:
    qs = []
    if "limit" in args:
        qs.append(f"limit={int(args['limit'])}")
    if args.get("status"):
        qs.append(f"status={quote(str(args['status']))}")
    suffix = ("?" + "&".join(qs)) if qs else ""
    return _request("GET", f"/notes{suffix}")


def _tool_get_latest_note(_args: dict) -> Any:
    return _request("GET", "/notes/latest")


def _tool_get_note(args: dict) -> Any:
    slug = quote(str(args["slug"]))
    return _request("GET", f"/notes/{slug}")


def _tool_get_note_status(args: dict) -> Any:
    slug = quote(str(args["slug"]))
    return _request("GET", f"/notes/{slug}/status")


def _tool_search_notes(args: dict) -> Any:
    payload = {
        "query": str(args.get("query", "")),
        "component": args.get("component", "any"),
        "limit": int(args.get("limit", 20)),
    }
    return _request("POST", "/notes/search", payload)


def _tool_start_recording(_args: dict) -> Any:
    return _request("POST", "/recordings/start", {})


def _tool_stop_recording(_args: dict) -> Any:
    return _request("POST", "/recordings/stop", {})


def _tool_recording_status(_args: dict) -> Any:
    return _request("GET", "/recordings/status")


def _tool_transcribe_file(args: dict) -> Any:
    return _request("POST", "/jobs/transcribe-file", {"path": str(args["path"])})


def _tool_get_job(args: dict) -> Any:
    job_id = quote(str(args["job_id"]))
    return _request("GET", f"/jobs/{job_id}")


def _tool_pause_processing(_args: dict) -> Any:
    return _request("POST", "/processing/pause", {})


def _tool_resume_processing(_args: dict) -> Any:
    return _request("POST", "/processing/resume", {})


def _tool_processing_status(_args: dict) -> Any:
    return _request("GET", "/processing/status")


TOOLS: list[tuple[Tool, callable]] = [
    (
        Tool(
            name="list_notes",
            description="List saved Muesli voice notes, newest first.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "default": 50},
                    "status": {"type": "string", "description": "Optional filter, e.g. 'done', 'error'."},
                },
            },
        ),
        _tool_list_notes,
    ),
    (
        Tool(
            name="get_latest_note",
            description="Get the most recent saved voice note.",
            inputSchema={"type": "object", "properties": {}},
        ),
        _tool_get_latest_note,
    ),
    (
        Tool(
            name="get_note",
            description="Get a saved voice note by slug.",
            inputSchema={
                "type": "object",
                "properties": {"slug": {"type": "string"}},
                "required": ["slug"],
            },
        ),
        _tool_get_note,
    ),
    (
        Tool(
            name="get_note_status",
            description="Get the processing status of one note (status, processing_stage).",
            inputSchema={
                "type": "object",
                "properties": {"slug": {"type": "string"}},
                "required": ["slug"],
            },
        ),
        _tool_get_note_status,
    ),
    (
        Tool(
            name="search_notes",
            description="Deterministic substring search over saved notes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "component": {
                        "type": "string",
                        "enum": ["any", "title", "summary", "transcript"],
                        "default": "any",
                    },
                    "limit": {"type": "integer", "minimum": 1, "default": 20},
                },
                "required": ["query"],
            },
        ),
        _tool_search_notes,
    ),
    (
        Tool(
            name="start_recording",
            description="Start recording from the default microphone.",
            inputSchema={"type": "object", "properties": {}},
        ),
        _tool_start_recording,
    ),
    (
        Tool(
            name="stop_recording",
            description="Stop the active recording. Finalization runs as an async job.",
            inputSchema={"type": "object", "properties": {}},
        ),
        _tool_stop_recording,
    ),
    (
        Tool(
            name="recording_status",
            description="Get live default-mic recording state.",
            inputSchema={"type": "object", "properties": {}},
        ),
        _tool_recording_status,
    ),
    (
        Tool(
            name="transcribe_file",
            description="Transcribe an existing audio file (does not save a managed note). Returns a job id.",
            inputSchema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        ),
        _tool_transcribe_file,
    ),
    (
        Tool(
            name="get_job",
            description="Get one async job by id (status, result when finished).",
            inputSchema={
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
        ),
        _tool_get_job,
    ),
    (
        Tool(
            name="pause_processing",
            description=(
                "Pause local Whisper transcription and summary work. Queued jobs "
                "stay queued and resume automatically when resume_processing is "
                "called. Natural-language aliases the agent should map to this "
                "tool: 'pause processing', 'stop the GPU work', "
                "'silence this machine', 'pause Muesli work', 'hold transcription'."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        _tool_pause_processing,
    ),
    (
        Tool(
            name="resume_processing",
            description=(
                "Resume local Whisper + summary work. One resume call drains "
                "every queued job (chunk pipeline, in-flight transcribe, "
                "scheduled reprocess). Natural-language aliases: 'resume "
                "processing', 'unpause Muesli', 'continue transcription', "
                "'wake the GPU'."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        _tool_resume_processing,
    ),
    (
        Tool(
            name="processing_status",
            description="Check whether local Whisper + summary work is currently paused.",
            inputSchema={"type": "object", "properties": {}},
        ),
        _tool_processing_status,
    ),
]

TOOL_INDEX = {tool.name: (tool, fn) for tool, fn in TOOLS}


# ── MCP server wiring ────────────────────────────────────────────────────────

server = Server("muesli")


@server.list_tools()
async def _list_tools() -> list[Tool]:
    return [tool for tool, _ in TOOLS]


@server.call_tool()
async def _call_tool(name: str, arguments: dict) -> list[TextContent]:
    entry = TOOL_INDEX.get(name)
    if not entry:
        return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]
    _, fn = entry
    try:
        result = fn(arguments or {})
    except ServiceUnavailable as exc:
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]
    except Exception as exc:
        return [TextContent(type="text", text=json.dumps({"error": f"{type(exc).__name__}: {exc}"}))]
    return [TextContent(type="text", text=json.dumps(result, default=str))]


async def _amain() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(_amain())
