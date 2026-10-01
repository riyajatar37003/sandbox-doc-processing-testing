"""Connection + tenant configuration for the NextWave client."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class NextWaveConfig:
    """Connection + tenant configuration (defaults mirror nw_sandbox.jmx)."""

    snc_host: str = "ppgenaiaus03.service-now.com"
    snc_protocol: str = "https"
    instance_name: str = "ppgenaiaus03"
    username: str = ""
    password: str = "Perftest@123"
    # Base32 TOTP secret for accounts enrolled with authenticator-app MFA — blank
    # skips the MFA step entirely (accounts with no MFA factor registered).
    totp_secret: str = ""
    oauth_client_id: str = "46e42d08770746f1802167828fcc6132"
    oauth_redirect_uri: str = "/api/snc/aiexauth/oauth/authorize"
    deployment_doc_id: str = "c86a62e2c7022010099a308dc7c26022"
    client_timezone: str = "Asia/Calcutta"
    verify_ssl: bool = True
    connect_timeout: int = 30
    request_timeout: int = 60
    stream_timeout: int = 1200
    # Per-read (socket) timeout while streaming SSE. `stream_timeout` is the
    # TOTAL wall-clock cap for a turn; this is the max gap allowed BETWEEN
    # bytes. Passing stream_timeout as the socket timeout meant a wedged stream
    # held the worker for the full 20 minutes — and `deadline` can't help,
    # because it is only evaluated when a line actually arrives.
    idle_timeout: int = 360
    # Overall wall-clock cap for the chat-session-create SSE stream. Some
    # instances hold that stream open without ever sending [DONE]; the session
    # payload arrives early, so we stop as soon as we have it (or hit this cap).
    session_timeout: int = 60
    max_events: int = 2000
    verbose: bool = False

    @property
    def base_url(self) -> str:
        return f"{self.snc_protocol}://{self.snc_host}"
