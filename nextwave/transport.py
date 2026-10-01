"""HTTP transport: a shared requests.Session plus the low-level SSE POST.

`SseTransport` is the one place that knows how to talk HTTP for this flow — it
owns the cookie jar, the browser-like default headers, the TLS policy, the
event emitter, and the streaming SSE reader. Auth / session / chatkit stages use
`transport.session` for plain calls and `transport.post_sse(...)` for streaming.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Optional

import requests
import urllib3

from .config import NextWaveConfig
from .models import NextWaveError, SessionState

EventCallback = Callable[[str, str, str, Optional[dict]], None]


class SseTransport:
    def __init__(
        self,
        cfg: NextWaveConfig,
        state: SessionState,
        on_event: Optional[EventCallback] = None,
    ):
        self.cfg = cfg
        self.state = state
        self._on_event: EventCallback = on_event or (lambda *_a, **_k: None)

        self.session = requests.Session()
        if not cfg.verify_ssl:
            self.session.verify = False
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.session.headers.update(
            {
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                # No 'br' — keeps us off the optional brotli dependency.
                "Accept-Encoding": "gzip, deflate",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/146.0.0.0 Safari/537.36"
                ),
            }
        )

    # ---- urls + observability ---------------------------------------------

    @property
    def base_url(self) -> str:
        return self.cfg.base_url

    def chat_base(self) -> str:
        if not self.state.chat_host:
            raise NextWaveError("chat_host not resolved — run session init first")
        return f"{self.cfg.snc_protocol}://{self.state.chat_host}"

    def log(self, msg: str) -> None:
        if self.cfg.verbose:
            print(msg, flush=True)

    def emit(self, level: str, channel: str, message: str, data: Optional[dict] = None) -> None:
        try:
            self._on_event(level, channel, message, data)
        except Exception:  # noqa: BLE001 — logging must never break the flow
            pass

    @property
    def timeouts(self) -> tuple[int, int]:
        return (self.cfg.connect_timeout, self.cfg.request_timeout)

    # ---- streaming SSE POST ------------------------------------------------

    def post_sse(
        self,
        url: str,
        payload: dict[str, Any],
        extra_headers: dict[str, str],
        timeout: Optional[int] = None,
        return_timing: bool = False,
        deadline: Optional[float] = None,
        stop_when: Optional[Callable[[list[dict[str, Any]]], bool]] = None,
    ) -> tuple[int, dict[str, str], list[dict[str, Any]], str]:
        """POST a JSON body and read the text/event-stream response.

        Returns (status_code, lowercased_response_headers, parsed_data_events,
        raw_body). When return_timing is set, the first event carries a
        `__ttfb_ms__` field with time-to-first-event in milliseconds.

        The reader normally ends on a `data: [DONE]` line or after `max_events`.
        Some endpoints hold the stream open and never send `[DONE]`, trickling
        keep-alive bytes so the per-read timeout never fires — to bound that:
          - `deadline` caps total wall-clock seconds spent reading the stream.
          - `stop_when(events)` is checked after each parsed event; returning
            True stops reading once the caller has what it needs.
        """
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.state.access_token}",
            "Origin": self.base_url,
            **extra_headers,
        }
        start = time.time()
        resp = self.session.post(
            url,
            json=payload,
            headers=headers,
            stream=True,
            timeout=(self.cfg.connect_timeout, timeout or self.cfg.request_timeout),
        )
        status = resp.status_code
        events: list[dict[str, Any]] = []
        raw_parts: list[str] = []
        first_event_ms = 0.0

        if status in (200, 201):
            current_event_name: Optional[str] = None
            for line in resp.iter_lines(decode_unicode=True):
                if deadline is not None and (time.time() - start) >= deadline:
                    self.emit(
                        "warn",
                        "message",
                        "sse read deadline reached",
                        {"deadline_s": deadline, "events": len(events)},
                    )
                    break
                if line is None:
                    continue
                raw_parts.append(line)
                stripped = line.strip()
                if stripped.startswith("event:"):
                    current_event_name = stripped[len("event:") :].strip() or None
                    continue
                if not stripped:
                    # blank line ends an SSE record — reset the event name for the next one
                    current_event_name = None
                    continue
                if not stripped.startswith("data:"):
                    continue
                data = stripped[len("data:") :].strip()
                if data == "[DONE]" or not data:
                    if data == "[DONE]":
                        break
                    continue
                if not first_event_ms:
                    first_event_ms = (time.time() - start) * 1000
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and current_event_name and "__sse_event__" not in obj:
                    obj["__sse_event__"] = current_event_name
                if return_timing and not events:
                    obj["__ttfb_ms__"] = first_event_ms
                events.append(obj)
                if len(events) >= self.cfg.max_events:
                    break
                if stop_when is not None and stop_when(events):
                    break
        else:
            raw_parts.append(resp.text)

        resp.close()
        headers_lc = {k.lower(): v for k, v in resp.headers.items()}
        return status, headers_lc, events, "\n".join(raw_parts)
