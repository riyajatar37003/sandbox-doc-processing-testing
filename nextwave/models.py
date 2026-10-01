"""Shared data models: runtime session state, per-turn result, and errors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


class NextWaveError(RuntimeError):
    """Raised on unrecoverable failures in the auth / session / chatkit flow."""


@dataclass
class SessionState:
    """Mutable runtime state shared across the flow stages.

    Populated incrementally: Authenticator sets the tokens, SessionInitializer
    resolves the chat host + ids, and ChatkitClient reads everything to talk to
    the conversation backend (and refreshes affinity headers per request).
    """

    g_ck: str = ""
    access_token: str = ""
    chat_host: str = ""
    consumer_account_id: str = ""
    user_id: str = ""
    session_id: str = ""
    pod_affinity: str = ""
    cluster_id: str = ""


@dataclass
class TurnResult:
    """Outcome of a single `send_message` turn."""

    user_message: str
    response_text: str
    raw_sse: str
    thread_id: str
    status_code: int
    response_time_ms: int
    ttfb_ms: int
    sse_event_count: int
    error: Optional[str] = None
    events: list[dict[str, Any]] = field(default_factory=list)
    # Uploaded attachment entries for this turn (each has at least name/mime_type/sys_id).
    attachments: list[dict[str, Any]] = field(default_factory=list)
    # Files the agent generated and sent back this turn (each has
    # sys_id/title/mime_type/size/download_url — see parsing.extract_file_attachments).
    file_attachments: list[dict[str, Any]] = field(default_factory=list)
    # True when the SSE stream stopped mid-turn (connection closed before the
    # real answer arrived) rather than finishing cleanly -- see
    # parsing.stream_ended_abnormally. A caller should treat this as a
    # retryable failure even though status_code/ok looks like success.
    stream_truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status_code < 300
