"""ChatKit stage: create threads and send user messages over the SSE protocol.

This is the part that actually invokes NextWave. `send_message` is the hot path
used by the eval harness — one fresh thread per utterance by default.
"""

from __future__ import annotations

import mimetypes
import os
import time
from typing import Any, Optional

from .config import NextWaveConfig
from .models import NextWaveError, SessionState, TurnResult
from .parsing import (
    dig,
    event_type_histogram,
    extract_assistant_text,
    extract_file_attachments,
    stream_ended_abnormally,
)
from .transport import SseTransport

_CHATKIT_PATH = "/conversation/api/chatkit"


class ChatkitClient:
    def __init__(self, cfg: NextWaveConfig, state: SessionState, transport: SseTransport):
        self.cfg = cfg
        self.state = state
        self.t = transport

    # ---- request metadata --------------------------------------------------

    def _metadata(self, conversation_id: str) -> dict[str, Any]:
        return {
            "conversationId": conversation_id,
            "webSearch": {"enabled": False},
            "sessionId": self.state.session_id,
            "instanceName": self.cfg.instance_name,
            "userId": self.state.user_id,
            "authToken": self.state.access_token,
            "consumerAccountId": self.state.consumer_account_id,
            "pageContext": {
                "type": "home",
                "route": "home",
                "experienceName": "workspace",
                "pageType": "UXF",
                "deploymentDocumentId": self.cfg.deployment_doc_id,
                "deploymentDocumentTable": "sys_ux_app",
            },
            "locationUrl": f"https://{self.cfg.snc_host}/now/nav/ui/home",
        }

    def _affinity_headers(self) -> dict[str, str]:
        extra = {"x-session-id": self.state.session_id}
        if self.state.pod_affinity:
            extra["x-pod-affinity"] = self.state.pod_affinity
        if self.state.cluster_id:
            extra["x-cluster-id"] = self.state.cluster_id
        return extra

    def _chatkit_url(self) -> str:
        return f"{self.t.chat_base()}{_CHATKIT_PATH}"

    # ---- create thread -----------------------------------------------------

    def create_thread(self) -> str:
        payload = {
            "type": "threads.create",
            "params": {},
            "metadata": self._metadata(""),
        }
        status, headers, events, raw = self.t.post_sse(
            self._chatkit_url(),
            payload,
            extra_headers={"x-session-id": self.state.session_id},
            # Idle-gap timeout, not the full-turn budget: threads.create should
            # answer in seconds, so don't let it sit on a dead socket.
            timeout=min(self.cfg.idle_timeout, 60),
            deadline=60,
        )
        if status not in (200, 201):
            self.t.emit("error", "thread", "threads.create failed", {"status": status})
            raise NextWaveError(f"threads.create failed: {status} {raw[:200]}")

        # Affinity headers must be echoed back on subsequent requests.
        self.state.pod_affinity = headers.get("x-pod-affinity", "") or self.state.pod_affinity
        self.state.cluster_id = headers.get("x-cluster-id", "") or self.state.cluster_id

        thread_id = ""
        for ev in events:
            if ev.get("object") == "thread" and ev.get("id"):
                thread_id = str(ev["id"])
            elif isinstance(ev.get("id"), str) and len(ev["id"]) == 32:
                thread_id = ev["id"]
            else:
                thread_id = dig(ev, "thread_id", ("data", "id"), ("thread", "id"), ("result", "id"))
            if thread_id:
                break
        if not thread_id:
            self.t.emit("error", "thread", "threads.create returned no thread id")
            raise NextWaveError(f"threads.create did not return a thread id: {raw[:200]}")
        self.t.log(f"[thread] created {thread_id}")
        self.t.emit("info", "thread", "thread created", {"thread_id": thread_id})
        return thread_id

    # ---- attachment upload -------------------------------------------------

    def upload_attachment(self, thread_id: str, file_path: str) -> dict[str, Any]:
        """Upload one file for a conversation and return its attachment object
        (the entry to place in `input.attachments`).

        Mirrors the lbf client: POST {chatkit}/upload?conversationId&sessionId&
        name&mimeType as multipart/form-data with the file under the ``data`` part.
        The server endpoint consumes multipart/form-data (Spring MultipartFile) and
        rejects a raw-bytes body with 415 Unsupported Media Type.
        """
        name = os.path.basename(file_path)
        mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        with open(file_path, "rb") as f:
            data = f.read()

        # NOTE: do not set Content-Type here — requests sets multipart/form-data
        # with the correct boundary when `files=` is provided. The file part is
        # named "data" (the controller also accepts "file" as an alternate).
        resp = self.t.session.post(
            f"{self._chatkit_url()}/upload",
            params={
                "conversationId": thread_id,
                "sessionId": self.state.session_id,
                "name": name,
                "mimeType": mime,
            },
            files={"data": (name, data, mime)},
            headers={
                "Authorization": f"Bearer {self.state.access_token}",
                "x-session-id": self.state.session_id,
                "Origin": self.t.base_url,
                **self._affinity_headers(),
            },
            timeout=(self.cfg.connect_timeout, self.cfg.stream_timeout),
        )
        if resp.status_code not in (200, 201):
            error_body = resp.text[:500] if resp.text else "(empty response body)"
            headers_info = f"content-type={resp.headers.get('content-type', 'none')}, content-length={resp.headers.get('content-length', 'none')}"
            raise NextWaveError(
                f"attachment upload failed: {resp.status_code} | "
                f"file={name} size={len(data)} mime={mime} | "
                f"response: {error_body} | headers: {headers_info}"
            )

        att = resp.json() or {}
        # Unwrap common envelopes.
        if isinstance(att.get("attachment"), dict):
            att = att["attachment"]
        elif isinstance(att.get("result"), dict):
            att = att["result"]

        # Normalize to the input.attachments entry shape (FileAttachment/ImageAttachment).
        entry: dict[str, Any] = dict(att)
        entry.setdefault("type", "image" if mime.startswith("image/") else "file")
        entry.setdefault("name", name)
        entry.setdefault("mime_type", mime)
        entry.setdefault("size", len(data))
        # Surface the server attachment id under a stable `sys_id` key (the upload
        # response may name it id / sys_id / attachment_id) for the eval transcript/trace.
        entry["sys_id"] = entry.get("sys_id") or entry.get("id") or entry.get("attachment_id") or ""
        return entry

    # ---- poll thread items (fallback for SSE-truncated turns) --------------

    def poll_thread_items(
        self,
        thread_id: str,
        max_polls: int = 8,
        poll_interval: float = 15.0,
    ) -> str:
        """Poll threads.list_items until an assistant_message appears.

        Used as a fallback when SSE stream ends without delivering an
        assistant_message (server-side streaming timeout with slow models like
        Gemini on large PDFs). The server finishes generating and stores the
        result in the thread item store even after the SSE connection closes."""
        payload = {
            "type": "threads.list_items",
            "params": {"thread_id": thread_id},
            "metadata": self._metadata(thread_id),
        }
        for attempt in range(1, max_polls + 1):
            time.sleep(poll_interval)
            try:
                status, _headers, events, _raw = self.t.post_sse(
                    self._chatkit_url(),
                    payload,
                    extra_headers=self._affinity_headers(),
                    timeout=self.cfg.idle_timeout,
                    deadline=60,
                )
                text = extract_assistant_text(events)
                self.t.emit(
                    "info",
                    "message",
                    "poll_thread_items",
                    {"thread_id": thread_id, "attempt": attempt, "status": status,
                     "n_events": len(events), "answer_len": len(text)},
                )
                if text:
                    return text
            except Exception as exc:
                self.t.emit("warn", "message", "poll_thread_items error", {"attempt": attempt, "error": str(exc)})
        return ""

    # ---- send message ------------------------------------------------------

    def send_message(
        self,
        user_message: str,
        thread_id: Optional[str] = None,
        attachment_files: Optional[list[str]] = None,
        max_retries: int = 1,
    ) -> TurnResult:
        """Send one utterance, optionally with file attachments. Creates a fresh
        thread when thread_id is omitted.

        Retries (re-posting the SAME utterance on the SAME thread) up to
        max_retries times when the stream cuts off mid-turn (see
        parsing.stream_ended_abnormally) -- status 200, no error, but the
        platform's own last event says it was still streaming and no real
        answer ever arrived. This only helps if the cutoff is intermittent; if
        every call for a given backend hits it, retrying will not fix that --
        the final TurnResult's `error` says so explicitly rather than leaving
        an unexplained empty response_text, so a systemic (not flaky) failure
        is never mistaken for "the agent had nothing to say"."""
        if thread_id is None:
            thread_id = self.create_thread()

        # Upload any attachments first (needs the thread/conversation id).
        # If upload fails, log the error but continue without the attachment.
        attachments: list[dict[str, Any]] = []
        for fp in attachment_files or []:
            try:
                attachments.append(self.upload_attachment(thread_id, fp))
                self.t.emit(
                    "info",
                    "message",
                    "attachment uploaded",
                    {"thread_id": thread_id, "file": os.path.basename(fp)},
                )
            except Exception as e:
                print(f"  *** UPLOAD FAILED: {os.path.basename(fp)} — {e}")
                self.t.emit(
                    "error",
                    "message",
                    "attachment upload failed - continuing without attachment",
                    {
                        "file": fp,
                        "error": str(e),
                        "thread_id": thread_id,
                    },
                )
                # Continue without this attachment rather than failing the entire utterance

        payload = {
            "type": "threads.add_user_message",
            "params": {
                "thread_id": thread_id,
                "input": {
                    "content": [{"type": "input_text", "text": user_message}],
                    "attachments": attachments,
                    "quoted_text": None,
                    "inference_options": {},
                },
            },
            "metadata": self._metadata(thread_id),
        }

        self.t.emit(
            "tool",
            "message",
            "add_user_message sent",
            {"thread_id": thread_id, "chars": len(user_message), "attachments": len(attachments)},
        )
        start = time.time()
        try:
            for attempt in range(max_retries + 1):
                status, _headers, events, raw = self.t.post_sse(
                    self._chatkit_url(),
                    payload,
                    extra_headers=self._affinity_headers(),
                    # timeout = max gap between bytes; deadline = total turn budget.
                    timeout=self.cfg.idle_timeout,
                    deadline=self.cfg.stream_timeout,
                    return_timing=True,
                )
                truncated = status in (200, 201) and stream_ended_abnormally(events)
                if not truncated or attempt == max_retries:
                    break
                self.t.emit(
                    "warn",
                    "message",
                    "stream ended mid-turn (no assistant_message ever arrived) -- retrying same utterance",
                    {"thread_id": thread_id, "attempt": attempt + 1, "sse_event_count": len(events)},
                )

            ttfb = events[0]["__ttfb_ms__"] if events and "__ttfb_ms__" in events[0] else 0
            text = extract_assistant_text(events)
            file_attachments = extract_file_attachments(events)
            elapsed_ms = int((time.time() - start) * 1000)
            if status in (200, 201) and not truncated:
                self.t.emit(
                    "info",
                    "message",
                    "reply received",
                    {
                        "thread_id": thread_id,
                        "ttfb_ms": int(ttfb),
                        "response_time_ms": elapsed_ms,
                        "sse_event_count": len(events),
                        "response_chars": len(text),
                        "event_types": event_type_histogram(events),
                    },
                )
            elif truncated:
                self.t.emit(
                    "error",
                    "message",
                    "stream ended mid-turn after retries exhausted -- backend/model integration "
                    "gap (connection closed before a real answer arrived), not a transient blip",
                    {"thread_id": thread_id, "sse_event_count": len(events)},
                )
            else:
                self.t.emit(
                    "error",
                    "message",
                    "add_user_message failed",
                    {"thread_id": thread_id, "status": status, "body": raw[:300]},
                )
            if truncated:
                # SSE stream closed before assistant_message arrived (server-side
                # timeout on slow models like Gemini with large PDFs). The server
                # still finishes generating and stores the result in the thread item
                # store — poll for it the same way the UI does.
                self.t.emit("info", "message", "SSE truncated — falling back to poll_thread_items",
                            {"thread_id": thread_id, "sse_event_count": len(events)})
                polled_text = self.poll_thread_items(thread_id)
                if polled_text:
                    self.t.emit("info", "message", "poll_thread_items recovered answer",
                                {"thread_id": thread_id, "chars": len(polled_text)})
                    text = polled_text
                    truncated = False
                    error = None
                else:
                    error = (f"STREAM_TRUNCATED: connection closed mid-turn before a real answer arrived "
                              f"(no assistant_message in {len(events)} event(s) received), not a normal "
                              f"empty reply -- likely a backend/model integration gap for whichever "
                              f"provider is active, not this client")
            else:
                error = None if status in (200, 201) else f"HTTP {status}"
            return TurnResult(
                user_message=user_message,
                response_text=text,
                raw_sse=raw,
                thread_id=thread_id,
                status_code=status,
                response_time_ms=elapsed_ms,
                ttfb_ms=int(ttfb),
                sse_event_count=len(events),
                error=error,
                events=events,
                attachments=attachments,
                file_attachments=file_attachments,
                stream_truncated=truncated,
            )
        except Exception as e:  # noqa: BLE001 — capture per-turn failure, keep eval going
            self.t.emit(
                "error",
                "message",
                "add_user_message raised",
                {"thread_id": thread_id, "error": str(e)},
            )
            return TurnResult(
                user_message=user_message,
                response_text="",
                raw_sse="",
                thread_id=thread_id or "",
                status_code=0,
                response_time_ms=int((time.time() - start) * 1000),
                ttfb_ms=0,
                sse_event_count=0,
                error=str(e),
                attachments=attachments,
            )
