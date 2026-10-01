"""Authentication stage: form login + OAuth 2.0 PKCE token exchange.

Sets `state.g_ck` (the instance CSRF/user token) and `state.access_token`
(the bearer token used for every conversation call).
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import uuid

try:
    import pyotp
except ImportError:  # pragma: no cover — only needed for TOTP-enrolled accounts
    pyotp = None

from .config import NextWaveConfig
from .models import NextWaveError, SessionState
from .transport import SseTransport

# Character set for the PKCE code verifier (RFC 7636 unreserved set).
_PKCE_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"


class Authenticator:
    def __init__(self, cfg: NextWaveConfig, state: SessionState, transport: SseTransport):
        self.cfg = cfg
        self.state = state
        self.t = transport

    def run(self) -> None:
        """Full auth: login, TOTP MFA challenge (if enrolled), then OAuth PKCE."""
        self.login()
        self.mfa_totp()
        self.oauth()

    # ---- form login --------------------------------------------------------

    def login(self) -> None:
        if not self.cfg.username:
            raise NextWaveError("config.username is required")

        r = self.t.session.get(
            f"{self.t.base_url}/login.do", timeout=self.t.timeouts, allow_redirects=True
        )
        m = re.search(r'name="sysparm_ck"[^>]*value="([^"]+)"', r.text)
        sysparm_ck = m.group(1) if m else "NOT_FOUND"

        r = self.t.session.post(
            f"{self.t.base_url}/login.do",
            data={
                "sysparm_ck": sysparm_ck,
                "user_name": self.cfg.username,
                "user_password": self.cfg.password,
                "sys_action": "sysverb_login",
                "ni.nolog.user_password": "true",
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": self.t.base_url,
            },
            timeout=self.t.timeouts,
            allow_redirects=True,
        )
        m = re.search(r"var g_ck = '(.*?)'", r.text)
        self.state.g_ck = m.group(1) if m else ""
        if not self.state.g_ck:
            self.t.emit(
                "error",
                "auth",
                "login failed — g_ck not found",
                {"username": self.cfg.username, "status": r.status_code},
            )
            raise NextWaveError(
                f"Login failed for '{self.cfg.username}' — g_ck not found (status {r.status_code})"
            )
        self.t.log(f"[login] g_ck acquired for {self.cfg.username}")
        self.t.emit("info", "auth", "login successful", {"username": self.cfg.username})
        self._last_login_response = r

    # ---- MFA (TOTP) ---------------------------------------------------------

    def mfa_totp(self) -> None:
        """Complete the TOTP challenge if this account has authenticator-app MFA
        enrolled. `login()`'s `g_ck` check alone can't detect this — the MFA
        challenge/setup pages embed a `g_ck` too, so a real MFA account still
        reports "g_ck acquired" while never actually completing authentication
        (confirmed via `X-Is-Logged-In: false` until this step runs).
        """
        r = getattr(self, "_last_login_response", None)
        if r is None or "validate_multifactor_auth_code.do" not in r.url:
            return  # no MFA challenge presented — account has no factor enrolled

        if not self.cfg.totp_secret or pyotp is None:
            self.t.emit(
                "error", "auth", "MFA challenge presented but no TOTP secret configured",
                {"username": self.cfg.username},
            )
            raise NextWaveError(
                "Account requires MFA (TOTP) but config.totp_secret is unset or pyotp is not installed"
            )

        m = re.search(r'<FORM name="validate_mfa_code"[^>]*>.*?</FORM>', r.text, re.S | re.I)
        if not m:
            raise NextWaveError("MFA challenge page found but validate_mfa_code form not present")
        form = m.group(0)

        def field(name: str) -> str:
            fm = re.search(rf'name="{name}"[^>]*value="([^"]*)"', form)
            return fm.group(1) if fm else ""

        page_ck = field("sysparm_ck")
        nonce = field("sysparm_process_nonce")

        # Mirrors submitMFACode() in the page's own JS: it sets a `factor` cookie
        # and several hidden fields to "false"/"TOTP" before submitting — the
        # server rejects the POST with a silent redirect to session_timeout.do
        # if any of these are missing, rather than validating the code at all.
        self.t.session.cookies.set("factor", "TOTP", domain=self.cfg.snc_host)
        code = pyotp.TOTP(self.cfg.totp_secret).now()

        r2 = self.t.session.post(
            f"{self.t.base_url}/validate_mfa_code.do",
            data={
                "sysparm_ck": page_ck,
                "sys_action": "sysverb_validate_mfa_code",
                "sysparm_process_nonce": nonce,
                "sys_mfa_check_remembered_browser": "false",
                "bfp": "",
                "bfp_hash": "",
                "sys_web_authentication_successful": "false",
                "sys_web_authentication_response": "",
                "sys_web_authn_registration_successful": "false",
                "sys_web_authn_registration_skipped": "false",
                "sys_mfa_factor_validate": "TOTP",
                "mfa_factor": "mfa_TOTP_div",
                "txtResponse": code,
                "remember_browser": "false",
                "register_authenticator_chk": "false",
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": self.t.base_url,
                "Referer": r.url,
            },
            timeout=self.t.timeouts,
            allow_redirects=True,
        )
        if "validate_multifactor_auth_code.do" in r2.url or "session_timeout.do" in r2.url:
            self.t.emit(
                "error", "auth", "TOTP validation failed",
                {"username": self.cfg.username, "final_url": r2.url},
            )
            raise NextWaveError(f"TOTP MFA validation failed — landed on {r2.url}")
        self.t.log(f"[mfa] TOTP challenge passed for {self.cfg.username}")
        self.t.emit("info", "auth", "MFA (TOTP) validated", {"username": self.cfg.username})

    # ---- oauth pkce --------------------------------------------------------

    def oauth(self) -> None:
        verifier = "".join(secrets.choice(_PKCE_CHARS) for _ in range(64))
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        state = str(uuid.uuid4())
        print(self.t.base_url)
        # GET auth code (302 -> Location carries ?code=...)
        r = self.t.session.get(
            f"{self.t.base_url}/oauth_auth.do",
            params={
                "response_type": "code",
                "client_id": self.cfg.oauth_client_id,
                "redirect_uri": self.cfg.oauth_redirect_uri,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
            },
            timeout=self.t.timeouts,
            allow_redirects=False,
        )
        location = r.headers.get("Location", "")
        m = re.search(r"code=([^&\s]+)", location) or re.search(r"code=([^&\s]+)", r.text)
        auth_code = m.group(1) if m else "NOT_FOUND"
        if auth_code == "NOT_FOUND":
            self.t.emit("error", "auth", "oauth auth code not found", {"status": r.status_code})
            raise NextWaveError(f"OAuth auth code not found (status {r.status_code})")

        # Hand the code to the AI experience auth endpoint (sets cookies).
        self.t.session.get(
            f"{self.t.base_url}{self.cfg.oauth_redirect_uri}",
            params={"code": auth_code, "state": state},
            timeout=self.t.timeouts,
            allow_redirects=True,
        )

        # Exchange code -> access token.
        r = self.t.session.post(
            f"{self.t.base_url}/oauth_token.do",
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "client_id": self.cfg.oauth_client_id,
                "redirect_uri": self.cfg.oauth_redirect_uri,
                "code_verifier": verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=self.t.timeouts,
            allow_redirects=True,
        )
        if r.status_code != 200:
            self.t.emit("error", "auth", "oauth_token.do failed", {"status": r.status_code})
            raise NextWaveError(f"oauth_token.do failed: {r.status_code} {r.text[:200]}")
        self.state.access_token = (r.json() or {}).get("access_token", "")
        if not self.state.access_token:
            self.t.emit("error", "auth", "oauth token exchange returned no access_token")
            raise NextWaveError("access_token missing from oauth_token.do response")
        self.t.log("[oauth] access_token acquired")
        self.t.emit("info", "auth", "oauth access_token acquired")
