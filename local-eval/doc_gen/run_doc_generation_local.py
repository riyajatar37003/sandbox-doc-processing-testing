#!/usr/bin/env python3
"""Local document-generation runner — the local-stack counterpart to doc_gen/run_doc_generation.py.

doc_gen/run_doc_generation.py drives a REMOTE ServiceNow instance via the shared
`nextwave` client (username/password auth). This script drives the SAME turn
sequence (extract key points, then generate a file) against your LOCAL docker
stack (conversation-server + agent-orchestrator-v2 on localhost:8040/8060) using
the ChatKit SSE protocol the browser UI itself uses — same session-capture
mechanism as local-eval/doc_qna/run_full_stack_eval.py, self-contained here.

PREREQUISITES
  1. Full stack running (conversation-server + agent-orchestrator-v2 + cache + valkey).
  2. One-time session capture: python3 init_session.py
  3. If calls fail with a bad/expired JWT: python3 refresh_jwt.py --refresh-deploy-test

USAGE
    # Full batch over datasets/sources/*.md, fresh timestamped output folder
    python3 run_doc_generation_local.py

    # One specific file
    python3 run_doc_generation_local.py --file datasets/sources/sample_housing_report.md

    # Multiple source files concurrently (one conversation per worker)
    python3 run_doc_generation_local.py --workers 4

    # Different source pattern and fixed/reusable output dir
    python3 run_doc_generation_local.py --source-dir datasets/sources \
      --output-dir datasets/generated --pattern "*.pdf"

The turn sequence lives in utterances.json (same shape as doc_gen/utterances-pdf.json)
— edit that file to change turns without touching this script.

Each run writes, into its output folder:
  - the generated file(s)
  - _run.log      — full console transcript, timestamped
  - _timing.json  — per-turn ttfb_ms/response_time_ms/elapsed_s, per-file elapsed_s + ok
"""
from __future__ import annotations

import argparse
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).parent
DEFAULT_UTTERANCES_FILE = HERE / "utterances.json"
DEFAULT_SOURCE_DIR = HERE / "datasets" / "sources"
SESSION_FILE = HERE / "session.json"

MIME = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".csv": "text/csv",
    ".pdf": "application/pdf",
}

OUT_MIME = {
    "pdf": "application/pdf",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

_lock = threading.Lock()


def log(msg: str, log_file: Path | None = None) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    with _lock:
        print(line)
        if log_file:
            with log_file.open("a") as f:
                f.write(line + "\n")


def _find_first(obj: Any, keys: tuple[str, ...]) -> str | None:
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and isinstance(obj[k], (str, int)):
                return str(obj[k])
        for v in obj.values():
            r = _find_first(v, keys)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_first(v, keys)
            if r:
                return r
    return None


def _content_text(content) -> str:
    if not isinstance(content, list):
        return ""
    return "".join(
        p.get("text", "") for p in content
        if isinstance(p, dict) and p.get("type") == "output_text" and isinstance(p.get("text"), str)
    )


def extract_final_text(events: list[dict]) -> str:
    """Join every assistant_message's output_text — mirrors doc_qna's extract_final_answer."""
    parts = []
    for ev in events:
        item = ev.get("item")
        if isinstance(item, dict) and item.get("type") == "assistant_message":
            t = _content_text(item.get("content"))
            if t:
                parts.append(t)
    return "\n".join(parts).strip()


def extract_file_attachment(events: list[dict]) -> dict | None:
    """Best-effort scan of every SSE event for a returned-file reference.

    UNVERIFIED SCHEMA: at the time this was written, every local file-generation
    turn failed upstream (missing GAIC mTLS client key -- see README "Known
    gotcha") before any file-bearing event was ever observed, so this cannot be
    tested against a real success payload yet. It scans broadly for the most
    plausible shapes (an attachment/content entry carrying an id + download hint,
    or a bare "download_url"/"file_id") rather than committing to one exact key
    path. If this returns None on a run where the assistant clearly produced a
    file, dump the raw .sse file and tighten this function to match reality.
    """
    def _scan(obj: Any) -> dict | None:
        if isinstance(obj, dict):
            keys = set(obj.keys())
            if {"download_url"} & keys or {"file_id", "download_url"} & keys:
                return obj
            if obj.get("type") in ("output_file", "file", "attachment") and (
                "id" in obj or "attachment_id" in obj or "download_url" in obj
            ):
                return obj
            for v in obj.values():
                r = _scan(v)
                if r:
                    return r
        elif isinstance(obj, list):
            for v in obj:
                r = _scan(v)
                if r:
                    return r
        return None

    for ev in events:
        item = ev.get("item")
        if not isinstance(item, dict):
            continue
        if item.get("type") != "assistant_message":
            continue
        hit = _scan(item)
        if hit:
            return hit
    return None


class ChatKitClient:
    """Minimal ChatKit SSE client -- same protocol as doc_qna/run_full_stack_eval.py,
    duplicated here deliberately so this script stays standalone (no cross-folder
    import), matching how doc_gen/ and qna_eval/ are each self-contained."""

    def __init__(self, base_url: str, session: dict, timeout: float):
        self.base = base_url.rstrip("/")
        self.session = session
        self.token = f"Bearer {session.get('authToken', 'test-token')}"
        self.device_id = session.get("customContext", {}).get("deviceId", str(uuid.uuid4()))
        self._local = threading.local()
        self._timeout = timeout

    @property
    def client(self) -> httpx.Client:
        if not hasattr(self._local, "client"):
            self._local.client = httpx.Client(timeout=self._timeout)
        return self._local.client

    def _headers(self, extra: dict | None = None) -> dict:
        h = {
            "Authorization": self.token,
            "X-Session-Id": self.session["sessionId"],
            "Cookie": f"sn-chatbot-deviceId={self.device_id}",
            "Origin": "http://localhost:8060",
        }
        if extra:
            h.update(extra)
        return h

    def _metadata(self, conversation_id: str) -> dict:
        m = dict(self.session)
        m["conversationId"] = conversation_id
        m["requestId"] = str(uuid.uuid4())
        return m

    def check_session(self) -> tuple[bool, str]:
        body = {"type": "threads.list", "params": {"order": "desc", "limit": 1},
                "metadata": self._metadata(self.session.get("conversationId", ""))}
        try:
            r = self.client.post(
                f"{self.base}/api/chatkit",
                headers=self._headers({"Content-Type": "application/json", "Accept": "application/json"}),
                json=body,
            )
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"
        return (r.status_code == 200), f"HTTP {r.status_code} {r.text[:160]}"

    def create_thread(self) -> str:
        body = {
            "type": "threads.create",
            "params": {"input": None, "initiateLiveAgent": False},
            "metadata": self._metadata(""),
        }
        r = self.client.post(
            f"{self.base}/api/chatkit",
            headers=self._headers({"Content-Type": "application/json", "Accept": "application/json"}),
            json=body,
        )
        r.raise_for_status()
        cid = _find_first(r.json(), ("conversationId", "thread_id", "threadId", "id"))
        if not cid:
            raise RuntimeError(f"No conversationId in threads.create response: {r.text[:400]}")
        return cid

    def upload(self, conversation_id: str, file_path: Path) -> dict:
        mime = MIME.get(file_path.suffix.lower(), "application/octet-stream")
        with file_path.open("rb") as fh:
            files = {"data": (file_path.name, fh, mime)}
            params = {
                "conversationId": conversation_id,
                "sessionId": self.session["sessionId"],
                "name": file_path.name,
                "mimeType": mime,
            }
            r = self.client.post(
                f"{self.base}/api/chatkit/upload",
                headers=self._headers(),
                params=params,
                files=files,
            )
        r.raise_for_status()
        data = r.json()
        if not data.get("id"):
            raise RuntimeError(f"No attachment id in upload response: {r.text[:400]}")
        return data

    def send_message_sse(
        self, conversation_id: str, attachments: list[dict], text: str, raw_dump: Path,
    ) -> tuple[str, list[dict]]:
        """threads.add_user_message over SSE -> (final_text, all_events)."""
        body = {
            "type": "threads.add_user_message",
            "params": {
                "thread_id": conversation_id,
                "input": {
                    "content": [{"type": "input_text", "text": text}],
                    # Pass the full raw upload() response through untouched -- mirrors
                    # nextwave/chatkit.py's send_message(), which does the same. A
                    # previous version of this file trimmed this to 4 keys and injected
                    # a "document_id": None that doesn't exist on the real response,
                    # which broke sandbox file-staging (the agent could no longer read
                    # the attachment at all, even though upload itself still succeeded).
                    "attachments": attachments,
                    "quoted_text": None,
                    "inference_options": {},
                },
            },
            "metadata": self._metadata(conversation_id),
        }
        events: list[dict] = []
        idle_timeout = 600
        initial_timeout = 300
        with self.client.stream(
            "POST",
            f"{self.base}/api/chatkit",
            headers=self._headers({"Content-Type": "application/json", "Accept": "text/event-stream"}),
            json=body,
        ) as resp:
            resp.raise_for_status()
            raw_lines = []
            stream_start = time.time()
            last_data_time = stream_start
            has_assistant_event = False
            for line in resp.iter_lines():
                if not line:
                    continue
                raw_lines.append(line)
                if line.startswith("data:"):
                    last_data_time = time.time()
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    if payload:
                        try:
                            ev = json.loads(payload)
                            events.append(ev)
                            etype = ev.get("type", "")
                            item_type = ev.get("item", {}).get("type", "") if isinstance(ev.get("item"), dict) else ""
                            if item_type not in ("user_message", "input_text", "") or "text_delta" in etype:
                                has_assistant_event = True
                            if etype == "thread.done":
                                break
                        except json.JSONDecodeError:
                            pass
                else:
                    if has_assistant_event and (time.time() - last_data_time) > idle_timeout:
                        break
                    if not has_assistant_event and (time.time() - stream_start) > initial_timeout:
                        break
            if raw_dump:
                ts = time.strftime("%Y%m%d_%H%M%S")
                stamped = raw_dump.with_stem(f"{raw_dump.stem}_{ts}")
                stamped.parent.mkdir(parents=True, exist_ok=True)
                with stamped.open("w") as f:
                    f.write("\n".join(raw_lines) + "\n")
        return extract_final_text(events), events

    def download(self, attachment_ref: dict) -> bytes:
        """Fetch a generated file's bytes.

        Tries, in order: an explicit download_url on the reference; the same
        sys_attachment.do pattern upload() receives back, keyed by id/attachment_id.
        """
        url = attachment_ref.get("download_url")
        if not url:
            att_id = attachment_ref.get("id") or attachment_ref.get("attachment_id") or attachment_ref.get("file_id")
            if not att_id:
                raise RuntimeError(f"No download_url or id in attachment reference: {attachment_ref}")
            url = f"{self.base}/api/chatkit/download?id={att_id}"
        r = self.client.get(url, headers=self._headers())
        r.raise_for_status()
        return r.content


def load_turns(utterances_file: Path) -> list[dict]:
    data = json.loads(utterances_file.read_text())
    turns = data["turns"] if isinstance(data, dict) else data
    for t in turns:
        t.setdefault("attach_source_file", False)
        t.setdefault("expect_file_attachment", False)
    return turns


def process_file(
    client: ChatKitClient, source_file: Path, turns: list[dict], out_dir: Path, debug_dir: Path,
) -> dict:
    log_file = out_dir / "_run.log"
    stem = source_file.stem
    log(f"=== {source_file.name} ===", log_file)
    t0 = time.time()
    timing: dict[str, Any] = {"file": source_file.name, "turns": []}

    try:
        conversation_id = client.create_thread()
    except Exception as e:  # noqa: BLE001
        log(f"  FAILED to create thread: {type(e).__name__}: {e}", log_file)
        timing["ok"] = False
        timing["error"] = str(e)
        return timing

    attachment = None
    last_text = ""
    ok = True
    for turn in turns:
        turn_t0 = time.time()
        label = turn.get("label", turn["id"])
        log(f"  Turn [{turn['id']}]: {label}...", log_file)

        attachments = []
        if turn.get("attach_source_file"):
            try:
                attachment = client.upload(conversation_id, source_file)
                attachments = [attachment]
            except Exception as e:  # noqa: BLE001
                log(f"  FAILED to upload source file: {type(e).__name__}: {e}", log_file)
                ok = False
                break

        try:
            text, events = client.send_message_sse(
                conversation_id, attachments, turn["utterance"],
                debug_dir / f"{stem}__{turn['id']}.sse",
            )
        except Exception as e:  # noqa: BLE001
            log(f"  FAILED turn '{turn['id']}': {type(e).__name__}: {e}", log_file)
            ok = False
            break
        last_text = text
        elapsed = time.time() - turn_t0
        timing["turns"].append({"id": turn["id"], "elapsed_s": round(elapsed, 1), "reply_chars": len(text)})
        log(f"    reply: {len(text)} chars, elapsed {elapsed:.1f}s", log_file)

        if text.strip().lower().startswith("i'm sorry, something went wrong"):
            log(f"    turn '{turn['id']}' returned the generic error fallback -- treating as failed", log_file)
            ok = False
            break

        if not turn.get("expect_file_attachment"):
            continue

        file_ref = extract_file_attachment(events)
        if not file_ref:
            log(f"    FAILED: turn '{turn['id']}' expected a file attachment but got none", log_file)
            ok = False
            break
        try:
            data = client.download(file_ref)
        except Exception as e:  # noqa: BLE001
            log(f"    FAILED to download attachment for turn '{turn['id']}': {type(e).__name__}: {e}", log_file)
            ok = False
            break
        ext = turn.get("save_as", "bin")
        out_path = out_dir / f"{stem}.{ext}"
        out_path.write_bytes(data)
        log(f"    saved -> {out_path} ({len(data)} bytes)", log_file)
        timing["output_file"] = str(out_path)
        timing["output_bytes"] = len(data)

    timing["ok"] = ok
    timing["elapsed_s"] = round(time.time() - t0, 1)
    timing["final_text"] = last_text[:500]
    return timing


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source-dir", default=str(DEFAULT_SOURCE_DIR))
    ap.add_argument("--pattern", default="*.md")
    ap.add_argument("--file", default=None, help="Process exactly one source file")
    ap.add_argument("--utterances-file", default=str(DEFAULT_UTTERANCES_FILE))
    ap.add_argument("--output-dir", default=None, help="Fixed output dir (default: timestamped under datasets/generated)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--base-url", default="http://localhost:8040")
    ap.add_argument("--session-file", default=str(SESSION_FILE))
    ap.add_argument("--check-session", action="store_true")
    ap.add_argument("--timeout", type=float, default=1200.0)
    args = ap.parse_args()

    session = json.loads(Path(args.session_file).read_text())
    client = ChatKitClient(args.base_url, session, args.timeout)

    if args.check_session:
        alive, detail = client.check_session()
        print(("SESSION ALIVE  " if alive else "SESSION EXPIRED  ") + detail[:200])
        raise SystemExit(0 if alive else 1)

    if args.file:
        files = [Path(args.file)]
    else:
        files = sorted(Path(args.source_dir).glob(args.pattern))
    if args.limit:
        files = files[: args.limit]
    if not files:
        raise SystemExit(f"No source files matched {args.source_dir}/{args.pattern}")

    out_dir = Path(args.output_dir) if args.output_dir else (
        HERE / "datasets" / "generated" / time.strftime("%Y%m%d_%H%M%S")
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    debug_dir = HERE / "debug"
    turns = load_turns(Path(args.utterances_file))

    print(f"[{time.strftime('%H:%M:%S')}] {len(files)} source file(s) -> {out_dir}")

    results = []
    if args.workers > 1:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(process_file, client, f, turns, out_dir, debug_dir): f for f in files}
            for fut in as_completed(futs):
                results.append(fut.result())
    else:
        for f in files:
            results.append(process_file(client, f, turns, out_dir, debug_dir))

    (out_dir / "_timing.json").write_text(json.dumps(results, indent=2))
    ok_count = sum(1 for r in results if r.get("ok"))
    print(f"Done. {ok_count}/{len(results)} succeeded -> {out_dir}")


if __name__ == "__main__":
    main()
