"""NextWaveClient — thin facade composing the auth / session / chatkit stages.

Public flow (unchanged from the original single-file client):

    client = NextWaveClient(cfg, on_event=logger.on_event).connect()
    result = client.send_message("create an incident")

Internally it wires four collaborators over one shared SessionState:
    transport  (SseTransport)        — HTTP + SSE plumbing, event emission
    auth       (Authenticator)       — login + OAuth PKCE
    session    (SessionInitializer)  — chat host + ids
    chatkit    (ChatkitClient)       — create_thread + send_message

Each collaborator is constructed with the same (cfg, state, transport), so any
one can be swapped or unit-tested in isolation.
"""

from __future__ import annotations

from typing import Optional

from .aia import AiaTrace, AiaTraceConfig, AiaTraceFetcher
from .auth import Authenticator
from .chatkit import ChatkitClient
from .config import NextWaveConfig
from .models import SessionState, TurnResult
from .session import SessionInitializer
from .transport import EventCallback, SseTransport


class NextWaveClient:
    def __init__(
        self,
        config: NextWaveConfig,
        on_event: Optional[EventCallback] = None,
        trace_cfg: Optional[AiaTraceConfig] = None,
    ):
        self.cfg = config
        # on_event(level, channel, message, data) — emitted at each lifecycle
        # step so callers can persist an execution trace. No-op by default.
        self._on_event: EventCallback = on_event or (lambda *_a, **_k: None)

        self.state = SessionState()
        self.transport = SseTransport(config, self.state, self._on_event)
        self.auth = Authenticator(config, self.state, self.transport)
        self.session_init = SessionInitializer(config, self.state, self.transport)
        self.chatkit = ChatkitClient(config, self.state, self.transport)
        self.aia = AiaTraceFetcher(config, self.transport, trace_cfg)

    # ---- lifecycle ---------------------------------------------------------

    def authenticate(self) -> None:
        """Run login + OAuth."""
        self.auth.run()

    def init_session(self) -> None:
        """Resolve the chat backend + open a chat session."""
        self.session_init.run()

    def connect(self) -> "NextWaveClient":
        """Convenience: authenticate + init_session, return self."""
        self.authenticate()
        self.init_session()
        return self

    # ---- conversation ------------------------------------------------------

    def create_thread(self) -> str:
        return self.chatkit.create_thread()

    def send_message(
        self,
        user_message: str,
        thread_id: Optional[str] = None,
        attachment_files: Optional[list[str]] = None,
    ) -> TurnResult:
        """Send one utterance (optionally with file attachments). Creates a fresh
        thread when thread_id is omitted."""
        return self.chatkit.send_message(
            user_message, thread_id=thread_id, attachment_files=attachment_files
        )

    def fetch_trace(self, conversation_id: str) -> AiaTrace:
        """Fetch the AIA execution trace (plan + tasks + tool executions) for a
        conversation. In this flow conversation_id is the turn's thread_id."""
        return self.aia.fetch(conversation_id)

    # ---- shared session (passthrough for convenience / back-compat) --------

    @property
    def requests_session(self):
        return self.transport.session

    def _emit(self, level: str, channel: str, message: str, data: Optional[dict] = None) -> None:
        self.transport.emit(level, channel, message, data)

    @property
    def g_ck(self) -> str:
        return self.state.g_ck

    @property
    def access_token(self) -> str:
        return self.state.access_token

    @property
    def chat_host(self) -> str:
        return self.state.chat_host

    @property
    def consumer_account_id(self) -> str:
        return self.state.consumer_account_id

    @property
    def user_id(self) -> str:
        return self.state.user_id

    @property
    def session_id(self) -> str:
        return self.state.session_id

    @property
    def pod_affinity(self) -> str:
        return self.state.pod_affinity

    @property
    def cluster_id(self) -> str:
        return self.state.cluster_id
