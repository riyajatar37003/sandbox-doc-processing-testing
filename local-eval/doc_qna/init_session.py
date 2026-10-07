#!/usr/bin/env python3
"""Capture a real session.json from the running LBF client (localhost:8060).

The LBF client's /chat/session endpoint returns session metadata including
the real sessionId, userId, instanceName, consumerAccountId, and pageContext
that the conversation server needs for tool execution routing.

Usage:
    python3 init_session.py                     # default: http://localhost:8060
    python3 init_session.py --base-url http://localhost:8060

If session.json already exists it is backed up to session.json.bak.
"""

import argparse
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
SESSION_FILE = HERE / "session.json"

# pageContext comes from the LBF client config, not the server.
# These are the default deployment IDs for the local dev setup.
DEFAULT_PAGE_CONTEXT = {
    "nowAssistDeploymentId": "7bd9820f14475650f87711349c1171be",
    "deploymentDocumentId": "c86a62e2c7022010099a308dc7c26022",
    "deploymentDocumentTable": "sys_ux_app",
}


def capture_session(base_url: str, timeout: float = 30.0) -> dict:
    """POST /chat/session (SSE stream) and extract the session block."""
    url = f"{base_url.rstrip('/')}/chat/session"
    device_id = str(uuid.uuid4())
    body = {
        "authToken": "test-token",
        "customContext": {"isFormDirty": False, "deviceId": device_id},
        "locationUrl": base_url + "/",
    }
    print(f"POST {url} (streaming)")
    session_data = {}
    with httpx.Client(timeout=timeout) as client:
        with client.stream("POST", url, json=body, headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        }) as resp:
            if resp.status_code >= 400:
                print(f"ERROR: HTTP {resp.status_code}")
                sys.exit(1)
            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    payload = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if "sessionId" in payload:
                    session_data = payload
                    break
                if "session" in payload and isinstance(payload["session"], dict):
                    session_data = payload["session"]
                    break

    if not session_data.get("sessionId"):
        print("ERROR: Could not extract sessionId from SSE stream.")
        sys.exit(1)

    # Build the session.json structure
    session = {
        "sessionId": session_data["sessionId"],
        "instanceName": session_data.get("instanceName") or "qnaaia1",
        "userId": session_data.get("userId", ""),
        "conversationId": session_data.get("conversationId", ""),
        "authToken": session_data.get("authToken", "test-token"),
        "consumerAccountId": session_data.get("consumerAccountId", ""),
        "pageContext": session_data.get("pageContext") or DEFAULT_PAGE_CONTEXT,
        "customContext": session_data.get("customContext", {"isFormDirty": False, "deviceId": device_id}),
        "locationUrl": session_data.get("locationUrl", base_url + "/"),
        "clientInstanceId": session_data.get("clientInstanceId", str(uuid.uuid4())),
        "applications": session_data.get("applications"),
        "clientContext": session_data.get("clientContext"),
    }
    return session


def main():
    ap = argparse.ArgumentParser(description="Capture session.json from the running LBF client")
    ap.add_argument("--base-url", default=os.getenv("CS_BASE_URL", "http://localhost:8040"),
                    help="conversation-server base URL (default: http://localhost:8040)")
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()

    print(f"Capturing session from {args.base_url} ...")
    session = capture_session(args.base_url, args.timeout)

    if SESSION_FILE.exists():
        bak = SESSION_FILE.with_suffix(".json.bak")
        shutil.copy2(SESSION_FILE, bak)
        print(f"Backed up existing session.json -> {bak.name}")

    SESSION_FILE.write_text(json.dumps(session, indent=2) + "\n")
    print(f"\nWrote {SESSION_FILE}")
    print(f"  sessionId:        {session['sessionId']}")
    print(f"  instanceName:     {session['instanceName']}")
    print(f"  userId:           {session['userId']}")
    print(f"  consumerAccountId:{session['consumerAccountId']}")
    print(f"  conversationId:   {session['conversationId']}")
    print(f"  pageContext:      {json.dumps(session.get('pageContext', {}))}")
    print("\nNext steps:")
    print("  1. Refresh JWT:   python3 refresh_jwt.py --refresh-deploy-test")
    print("  2. Check session: python3 run_full_stack_eval.py --check-session")
    print("  3. Run eval:      python3 run_full_stack_eval.py --category office --limit 1")


if __name__ == "__main__":
    main()
