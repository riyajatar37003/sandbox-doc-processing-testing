#!/usr/bin/env python3
"""Trace the full lifecycle of a single request through the ao-v2 pipeline.

The ao-v2 container emits one JSON object per log line, e.g.:
    {"timestamp": "...", "level": "INFO", "logger": "...",
     "message": "[DARE] Executing bash command in sandbox",
     "conversation_id": "7993e8...", "command_length": 733, ...}

This script filters those lines by ``conversation_id`` and reconstructs an
ordered timeline: every step, the tool/skill involved, its inputs/outputs
(the extra structured fields), and per-step + cumulative timing.

Sources (pick one):
  --conv <id>                    read from `docker logs <container>` (default)
  --file <path>                  read from a captured log file
  (stdin)                        `docker logs ... | trace_request.py --conv <id> -`

Examples:
  # After running an eval case, trace it by conversation id:
  python3 trace_request.py --conv 7993e81e3b5a8f5051a1da6eb5e45ad2

  # Only tool / skill / model events (skip framework noise):
  python3 trace_request.py --conv <id> --tools-only

  # From a saved capture:
  python3 trace_request.py --conv <id> --file /tmp/aov2_trace.log

  # Live follow (Ctrl-C to stop):
  python3 trace_request.py --conv <id> --follow
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime

DEFAULT_CONTAINER = "conversation-server-agent-orchestrator-v2-1"

# Fields present on (almost) every structured log line — hidden from the
# per-step "detail" column because they are constant noise for a single conv.
_NOISE_KEYS = {
    "timestamp", "level", "log_level", "logger", "message",
    "trace_id", "span_id", "parent_span_id", "conversation_id",
    "instance_name", "request_id", "worker_pid", "message_key",
}

# Huge structured values that bloat the trace (full LLM payloads, chat history,
# request bodies). Always rendered on their own line, not inline.
_BLOB_KEYS = {
    "payload", "body", "messages", "request_headers", "request_body",
    "headers", "__chat_history", "chat_history", "response_json",
    "command", "code", "stdout", "stderr", "output", "description_text",
}

# Blob fields that are source code / program output — rendered as an indented
# multi-line block with real newlines instead of a single escaped line.
_CODE_KEYS = {"command", "code", "stdout", "stderr", "output"}

# Repetitive GAIC plumbing lines that add no lifecycle signal — dropped unless
# --all is passed.
_NOISE_MESSAGES = (
    "[GAIC] messages",
    "[GAIC] Built payload history",
    "[GAIC] Using MOSAIC_BASE_URL",
    "[GAIC] request prepared",
    "[GAIC] astreaming request headers",
)

# Substrings that mark the interesting lifecycle events. Anything matching is
# always shown (even in --tools-only mode) and gets a category tag + colour.
_EVENT_TAGS: list[tuple[str, str]] = [
    ("CS turn received", "TURN"),
    ("CS turn", "TURN"),
    ("skill tool invoked", "SKILL"),
    ("skill loaded", "SKILL"),
    ("skill not found", "SKILL"),
    ("Skill resources mounted", "SKILL"),
    ("skill execution invoked", "SKILL"),
    ("skill unfilled mandatory inputs", "SKILL"),
    ("Executing tool with identifier", "TOOL"),
    ("Executing tool by M2M ID", "TOOL"),
    ("tool execution response built", "TOOL"),
    ("Tool execution successful", "TOOL"),
    ("Tool execution failed", "TOOL"),
    ("Executing bash command in sandbox", "BASH"),
    ("bash command source", "BASH"),
    ("bash command output", "BASH"),
    ("Bash command completed", "BASH"),
    ("view_image read image bytes", "VISION"),
    ("view_image: description received", "VISION"),
    ("view_image", "VISION"),
    ("[API] Tool inputs", "TOOL"),
    ("[API] Result", "TOOL"),
    ("[API] Metadata", "TOOL"),
    ("[GAIC] request body", "GAIC"),
    ("astreaming LLM call", "GAIC"),
    ("Filtered tools for sub-agent", "ROUTE"),
    ("Filtered already-executed tools", "ROUTE"),
    ("on_tool_start", "TOOL"),
    ("Executing deferred tool", "TOOL"),
    ("Executing approved tool", "TOOL"),
]

_CAT_COLOUR = {
    "TURN": "\033[1;97m",    # bright white
    "SKILL": "\033[1;35m",   # magenta
    "TOOL": "\033[1;36m",    # cyan
    "BASH": "\033[1;33m",    # yellow
    "VISION": "\033[1;32m",  # green
    "GAIC": "\033[1;34m",    # blue
    "ROUTE": "\033[0;90m",   # grey
    "": "\033[0m",
}
_RESET = "\033[0m"
_DIM = "\033[2m"
_RED = "\033[1;31m"

# Per-log-level colour for the LEVEL column.
_LEVEL_COLOUR = {
    "DEBUG": "\033[0;90m",     # grey
    "INFO": "\033[0;32m",      # green
    "WARNING": "\033[1;33m",   # yellow
    "ERROR": "\033[1;31m",     # red
    "CRITICAL": "\033[1;41m",  # red background
}


def _classify(message: str) -> str:
    for needle, tag in _EVENT_TAGS:
        if needle in message:
            return tag
    return ""


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _iter_lines(args: argparse.Namespace):
    """Yield raw log lines from the chosen source."""
    if args.file:
        with open(args.file, encoding="utf-8", errors="replace") as fh:
            yield from fh
        return
    if args.stdin:
        yield from sys.stdin
        return
    # docker logs
    cmd = ["docker", "logs"]
    if args.follow:
        cmd.append("-f")
    if args.since:
        cmd += ["--since", args.since]
    cmd.append(args.container)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        assert proc.stdout is not None
        yield from proc.stdout
    finally:
        if args.follow:
            proc.terminate()


def _fmt_detail(rec: dict, max_val: int, max_blob: int) -> tuple[str, list[tuple[str, str]]]:
    """Split structured fields into (inline small k=v string, [(key, blob), ...]).

    Small scalar fields render inline on one line; large payload/body/history
    blobs are returned separately so each can be printed on its own line.
    """
    inline = []
    blobs: list[tuple[str, str]] = []
    for k, v in rec.items():
        if k in _NOISE_KEYS:
            continue
        sval = v if isinstance(v, str) else json.dumps(v, default=str)
        if k in _BLOB_KEYS or len(sval) > max_val:
            # Source code / program output is shown in full (that's the point of
            # a trace); other blobs respect --max-blob.
            if k in _CODE_KEYS or max_blob <= 0:
                blob = sval
            else:
                blob = sval if len(sval) <= max_blob else sval[: max_blob - 1] + "…"
            blobs.append((k, blob))
        else:
            inline.append(f"{k}={sval}")
    return "  ".join(inline), blobs


def build_trace(
    lines,
    conversation_id: str,
    *,
    tools_only: bool = False,
    include_all: bool = False,
    errors_only: bool = False,
    max_detail: int = 160,
    max_blob: int = 1200,
    color: bool = True,
) -> str:
    """Render the lifecycle trace for one conversation as a string.

    ``lines`` is any iterable of raw JSON log lines (from a file, ``docker
    logs`` output, or stdin). Returns the formatted, optionally ANSI-coloured
    trace. Reusable from the eval harness so each case can save its own trace
    next to the .sse dump.
    """
    def paint(text: str, colour: str) -> str:
        return text if not color else f"{colour}{text}{_RESET}"

    out: list[str] = []
    t0: datetime | None = None
    prev: datetime | None = None
    n_shown = 0
    counts: dict[str, int] = {}

    out.append(paint(f"── Trace for conversation {conversation_id} ──", _CAT_COLOUR["TURN"]))
    out.append(f"{'ELAPSED':>9} {'+Δms':>7}  {'LEVEL':<5} {'CAT':<6} MESSAGE")
    out.append("-" * 100)

    for line in lines:
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("conversation_id") != conversation_id:
            continue

        level = rec.get("level", rec.get("log_level", ""))
        if errors_only and level not in ("WARNING", "ERROR", "CRITICAL"):
            continue

        message = rec.get("message", "")
        is_noise_msg = any(n in message for n in _NOISE_MESSAGES)
        if is_noise_msg and not include_all:
            continue
        cat = _classify(message)
        if tools_only and not cat:
            continue

        ts = _parse_ts(rec.get("timestamp", ""))
        if t0 is None and ts is not None:
            t0 = ts
        elapsed_ms = int((ts - t0).total_seconds() * 1000) if (ts and t0) else 0
        delta_ms = int((ts - prev).total_seconds() * 1000) if (ts and prev) else 0
        if ts is not None:
            prev = ts

        counts[cat or "other"] = counts.get(cat or "other", 0) + 1
        n_shown += 1

        colour = _CAT_COLOUR.get(cat, "")
        level_str = paint(f"{level[:5]:<5}", _LEVEL_COLOUR.get(level, ""))
        cat_str = paint(f"{cat:<6}", colour) if cat else f"{'':<6}"
        msg_str = paint(message, colour) if cat else message

        inline, blobs = _fmt_detail(rec, max_detail, max_blob)
        out.append(f"{elapsed_ms:>7}ms {('+' + str(delta_ms)):>7}  {level_str} {cat_str} {msg_str}")
        indent = f"{'':>19}  {'':<5} {'':<6} "
        if inline:
            out.append(f"{indent}{paint(inline, _DIM)}")
        for bk, bv in blobs:
            if bk in _CODE_KEYS and ("\n" in bv or len(bv) > 80):
                # Render code / output as a fenced, line-numbered block.
                out.append(f"{indent}{paint('⤷ ' + bk + ' ┌' + '─' * 60, colour or _DIM)}")
                code_lines = bv.split("\n")
                for ln_no, code_ln in enumerate(code_lines, 1):
                    out.append(f"{indent}{paint('│', colour or _DIM)} {paint(f'{ln_no:>3}', _DIM)} {code_ln}")
                out.append(f"{indent}{paint('└' + '─' * 62, colour or _DIM)}")
            else:
                out.append(f"{indent}{paint('⤷ ' + bk + ':', colour or _DIM)} {paint(bv, _DIM)}")

    out.append("-" * 100)
    total_ms = int((prev - t0).total_seconds() * 1000) if (prev and t0) else 0
    summary = "  ".join(f"{k}:{v}" for k, v in sorted(counts.items()))
    out.append(paint(f"Shown {n_shown} lines | total span {total_ms}ms | {summary}", _CAT_COLOUR["TURN"]))
    if n_shown == 0:
        out.append(paint("No lines matched — is the conversation_id correct and still in the log window? "
                         "Try --since 30m or capture logs to a file during the run.", _RED))
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--conv", "--conversation-id", dest="conv", required=True,
                    help="conversation_id to trace")
    ap.add_argument("--container", default=DEFAULT_CONTAINER,
                    help=f"docker container name (default: {DEFAULT_CONTAINER})")
    ap.add_argument("--file", default="", help="read from a captured log file instead of docker logs")
    ap.add_argument("-", dest="stdin", action="store_true", help="read log lines from stdin")
    ap.add_argument("--follow", action="store_true", help="live-follow docker logs (Ctrl-C to stop)")
    ap.add_argument("--since", default="", help="docker logs --since value (e.g. '10m', '2026-07-27T11:00:00')")
    ap.add_argument("--tools-only", action="store_true",
                    help="show only TURN/SKILL/TOOL/BASH/VISION/GAIC/ROUTE events (hide framework noise)")
    ap.add_argument("--all", action="store_true",
                    help="include repetitive GAIC plumbing lines (full payloads still truncated)")
    ap.add_argument("--errors-only", action="store_true", help="show only WARNING/ERROR lines")
    ap.add_argument("--max-detail", type=int, default=160,
                    help="a field longer than this is treated as a payload/blob and printed on its own line")
    ap.add_argument("--max-blob", type=int, default=1200,
                    help="truncate each payload/blob to N chars (0 = no truncation, print full payload)")
    ap.add_argument("--full", action="store_true", help="print full untruncated payloads (same as --max-blob 0)")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    args = ap.parse_args()
    if args.full:
        args.max_blob = 0

    trace = build_trace(
        _iter_lines(args),
        args.conv,
        tools_only=args.tools_only,
        include_all=args.all,
        errors_only=args.errors_only,
        max_detail=args.max_detail,
        max_blob=args.max_blob,
        color=not args.no_color,
    )
    print(trace)


if __name__ == "__main__":
    main()
