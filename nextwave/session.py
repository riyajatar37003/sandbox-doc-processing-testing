"""Session-init stage: resolve the chat backend host and open a chat session.

Discovers `chat_host` from session_info, opens the Now Assist panel channel,
then POSTs to `/conversation/chat/session` (SSE) to obtain the consumer account
id, user id, and session id used by every subsequent chatkit call.
"""

from __future__ import annotations

import re

from .config import NextWaveConfig
from .models import NextWaveError, SessionState
from .parsing import dig
from .transport import SseTransport


class SessionInitializer:
    def __init__(self, cfg: NextWaveConfig, state: SessionState, transport: SseTransport):
        self.cfg = cfg
        self.state = state
        self.t = transport

    def run(self) -> None:
        self._resolve_chat_host()
        self._open_channel()
        self._create_chat_session()

    # ---- steps -------------------------------------------------------------

    def _resolve_chat_host(self) -> None:
        r = self.t.session.get(
            f"{self.t.base_url}/api/now/nextwave_client/session_info",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.state.access_token}",
            },
            timeout=self.t.timeouts,
        )
        backend_url = ((r.json() or {}).get("result") or {}).get("backendUrl", "")
        if not backend_url:
            raise NextWaveError(f"backendUrl not found in session_info: {r.text[:200]}")
        self.state.chat_host = re.sub(r"^https?://", "", backend_url).split("/")[0]
        self.t.log(f"[session] chat_host = {self.state.chat_host}")
        self.t.emit("info", "session", "chat backend resolved", {"chat_host": self.state.chat_host})

    def _open_channel(self) -> None:
        self.t.session.get(
            f"{self.t.base_url}/api/now/v1/cs/console/channel",
            params={"channelName": "Now Assist Panel"},
            headers={"Content-Type": "application/json", "x-usertoken": self.state.g_ck},
            timeout=self.t.timeouts,
        )

    @staticmethod
    def _extract_session_ids(events: list[dict]):
        """Scan SSE events for the chat-session identifiers. Returns the
        (consumer_account_id, user_id, session_id) triple once all three are
        present, else None. IDs may be spread across multiple events."""
        account = user = session = ""
        for ev in events:
            account = account or dig(
                ev,
                "consumer_account_id",
                "consumerAccountId",
                ("data", "consumer_account_id"),
                ("result", "consumer_account_id"),
            )
            uid = dig(ev, "user_id", ("data", "user_id"), ("result", "user_id"))
            if not uid and isinstance(ev.get("userId"), str) and len(ev["userId"]) > 10:
                uid = ev["userId"]
            user = user or uid
            session = session or dig(
                ev,
                "sessionId",
                ("data", "sessionId"),
                ("result", "sessionId"),
            )
        if account and user and session:
            return account, user, session
        return None

    def _create_chat_session(self) -> None:
        payload = {
            "instance": self.cfg.instance_name,
            "userId": "",
            "conversationId": "",
            "clientTimezone": self.cfg.client_timezone,
            "experienceId": self.cfg.deployment_doc_id,
            "pageContext": {
                "type": "other",
                "experienceName": "workspace",
                "pageType": "UXF",
                "deploymentDocumentId": self.cfg.deployment_doc_id,
                "deploymentDocumentTable": "sys_ux_app",
            },
            "widgetContext": {},
            "customContext": {},
            "clientToolContext": {},
        }

        # This endpoint may hold the SSE stream open without ever sending
        # [DONE]; the account/user/session ids arrive in the first event(s), so
        # stop as soon as we have all three (or hit the session deadline).
        def _have_session(events: list[dict]) -> bool:
            return bool(self._extract_session_ids(events))

        status, headers, events, _raw = self.t.post_sse(
            f"{self.t.chat_base()}/conversation/chat/session",
            payload,
            extra_headers={},
            deadline=self.cfg.session_timeout,
            stop_when=_have_session,
        )
        if status not in (200, 201):
            raise NextWaveError(f"chat/session failed: {status}")

        self.state.pod_affinity = headers.get("x-pod-affinity", "") or self.state.pod_affinity
        self.state.cluster_id = headers.get("x-cluster-id", "") or self.state.cluster_id

        ids = self._extract_session_ids(events)
        if ids:
            self.state.consumer_account_id, self.state.user_id, self.state.session_id = ids

        if not (self.state.consumer_account_id and self.state.user_id and self.state.session_id):
            raise NextWaveError(
                "chat/session did not yield all of consumer_account_id/user_id/session_id "
                f"(got account={bool(self.state.consumer_account_id)} "
                f"user={bool(self.state.user_id)} session={bool(self.state.session_id)})"
            )
        self.t.log(
            f"[session] session_id={self.state.session_id} user_id={self.state.user_id} "
            f"account={self.state.consumer_account_id}"
        )
        self.t.emit(
            "info",
            "session",
            "session ready",
            {
                "session_id": self.state.session_id,
                "user_id": self.state.user_id,
                "consumer_account_id": self.state.consumer_account_id,
                "pod_affinity": self.state.pod_affinity,
                "cluster_id": self.state.cluster_id,
            },
        )
