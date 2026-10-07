import argparse
import base64
import datetime
import json
import os
import re
import subprocess
import time

import requests

CERTS_DIR = os.path.expanduser("~/Library/CloudStorage/OneDrive-ServiceNow/offglide-services/agent-orchestrator-v2/va_agentic/configs/mosaic_certs_lab")
COMPOSE_CWD = "/Users/riyaj.atar/Library/CloudStorage/OneDrive-ServiceNow/offglide-services/conversation-server"
ENV_FILE = "/Users/riyaj.atar/Library/CloudStorage/OneDrive-ServiceNow/offglide-services/agent-orchestrator-v2/.env"
CONTAINER_NAME = "conversation-server-agent-orchestrator-v2-1"


def _read_mosaic_url_from_env() -> str:
    """Build MOSAIC_URL from MOSAIC_BASE_URL and MOSAIC_API in the .env file."""
    base_url = ""
    api_path = "/api/mosaic/v1/execute"
    with open(ENV_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if line.startswith("MOSAIC_BASE_URL=") and not line.startswith("#"):
                base_url = line.split("=", 1)[1].strip()
            elif line.startswith("MOSAIC_API=") and not line.startswith("#"):
                api_path = line.split("=", 1)[1].strip()
    if not base_url:
        base_url = "https://mosaic-snc-prod-carthagelab004.dvb402.service-now.com"
        print(f"WARNING: MOSAIC_BASE_URL not found in .env, using default: {base_url}")
    return f"{base_url}{api_path}"


MOSAIC_URL = _read_mosaic_url_from_env()


def test_mosaic_curl(jwt_token: str):
    """Test the JWT against Mosaic API using the same certs as sample-curl-request.sh."""
    print("\n--- Testing JWT against Mosaic API ---")
    payload = {
        "capabilityRequests": [{
            "payload": {
                "textToSummarize": "What is the capital city of India? Answer in one sentence."
            },
            "capabilityId": "da7e00ca1bfdb210b4f480c643e45001"
        }]
    }
    result = subprocess.run(
        [
            "curl", "-s", "-w", "\n%{http_code}",
            "--key", os.path.join(CERTS_DIR, "nextwavecs-mock.key.pem"),
            "--cert", os.path.join(CERTS_DIR, "nextwavecs-mock.chain.pem"),
            "--cacert", os.path.join(CERTS_DIR, "server-trusted.pem"),
            "-H", "Content-Type: application/json",
            "-H", f"Glide-JWT: {jwt_token}",
            "-X", "POST",
            "-d", json.dumps(payload),
            MOSAIC_URL
        ],
        capture_output=True, text=True, timeout=30
    )
    output = result.stdout.strip()
    stderr = result.stderr.strip()
    lines = output.rsplit("\n", 1)
    status_code = lines[-1] if len(lines) > 1 else "unknown"
    body = lines[0] if len(lines) > 1 else output

    if stderr:
        print(f"curl stderr: {stderr[:500]}")
    if status_code == "200":
        print(f"Mosaic API: 200 OK")
        try:
            parsed = json.loads(body)
            print(f"Response preview: {json.dumps(parsed, indent=2)[:500]}")
        except json.JSONDecodeError:
            print(f"Response preview: {body[:300]}")
    else:
        print(f"Mosaic API: HTTP {status_code} FAILED")
        print(f"Response body: {body[:1000]}")
        print(f"Raw stdout: {output[:500]}")
    return status_code == "200"


def get_auth_token_instance(instance_name: str, user_name: str = 'off_glide_admin', password: str = 'Snow@2005'):
    url = f'https://{instance_name}.servicenowlab.com/api/now/ais_auth_token_refresh'
    user = user_name
    pwd = password
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    response = requests.post(url, auth=(user, pwd), headers=headers, json={
        "conversationId": "4b2925073b8e321051a1da6eb5e45a50",
        "requestId": "conversation-server.182757546.dfd584d5-85",
        "userId": "3c6ad5643bfdb21051a1da6eb5e45a63",
        "generate_mosaic_token": True
    })
    if response.status_code != 200:
        print('Status:', response.status_code, 'Headers:', response.headers, 'Error Response:', response.json())
        exit(1)
    return response.json()


def extract_token(data: dict) -> str:
    """Extract JWT token from the API response."""
    result = data.get("result", {})
    token = result.get("token") or result.get("glide_jwt") or result.get("jwtToken")
    if not token and isinstance(result.get("result"), dict):
        inner = result["result"]
        token = inner.get("token") or inner.get("glide_jwt") or inner.get("jwtToken")
    if not token:
        flat = str(data)
        match = re.search(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", flat)
        if match:
            token = match.group(0)
            print("Extracted JWT via regex")
    return token


def check_jwt_expiry(jwt_token: str):
    """Decode JWT and print how long until it expires."""
    parts = jwt_token.split(".")
    if len(parts) < 2:
        print("Invalid JWT format")
        return False
    payload = parts[1]
    payload += "=" * (4 - len(payload) % 4)
    decoded = json.loads(base64.urlsafe_b64decode(payload))
    exp = decoded.get("exp", 0)
    iat = decoded.get("iat", 0)
    exp_dt = datetime.datetime.utcfromtimestamp(exp)
    iat_dt = datetime.datetime.utcfromtimestamp(iat)
    now_dt = datetime.datetime.utcnow()
    print(f"Issued:  {iat_dt} UTC")
    print(f"Expires: {exp_dt} UTC")
    print(f"Now:     {now_dt} UTC")
    if now_dt > exp_dt:
        elapsed = (now_dt - exp_dt).total_seconds()
        print(f">>> EXPIRED {elapsed:.0f}s ago ({elapsed/60:.1f} min)")
        return False
    remaining = (exp_dt - now_dt).total_seconds()
    hours = int(remaining // 3600)
    minutes = int((remaining % 3600) // 60)
    print(f">>> Valid for {hours}h {minutes}m ({remaining:.0f}s)")
    return True


def read_current_jwt_from_env() -> str:
    """Read current JWT from .env file."""
    with open(ENV_FILE, "r") as f:
        content = f.read()
    match = re.search(r'LOCAL_TEMP_JWT_TOKEN_FOR_MOSAIC_LAB=(.*)', content)
    return match.group(1).strip() if match else ""

INSTANCE_NAME = "cidtest1nextmaria2"
def refresh_jwt():
    """Fetch new JWT and update .env file. Returns the new token."""
    print(f"Fetching fresh JWT from {INSTANCE_NAME}...")
    data = get_auth_token_instance(INSTANCE_NAME, 'maint', 'maint')
    token = extract_token(data)
    if not token:
        print("Could not extract token from response:")
        print(json.dumps(data, indent=2)[:2000])
        exit(1)
    print(f"Got token: {token[:60]}...")

    with open(ENV_FILE, "r") as f:
        content = f.read()

    pattern = r'(LOCAL_TEMP_JWT_TOKEN_FOR_MOSAIC_LAB=).*'
    if re.search(pattern, content):
        new_content = re.sub(pattern, f'\\1{token}', content)
    else:
        new_content = content + f'\nLOCAL_TEMP_JWT_TOKEN_FOR_MOSAIC_LAB={token}\n'

    with open(ENV_FILE, "w") as f:
        f.write(new_content)
    print(f"Updated {ENV_FILE} with new JWT")
    check_jwt_expiry(token)
    return token


def deploy_to_docker(token: str):
    """Recreate container with new env and verify."""
    print("Recreating agent-orchestrator-v2 (compose up -d)...")
    result = subprocess.run(
        ["docker", "compose", "--profile", "dare", "up", "-d", "agent-orchestrator-v2"],
        cwd=COMPOSE_CWD,
        capture_output=True, text=True
    )
    print(result.stdout)
    if result.returncode != 0:
        print("Compose up error:", result.stderr)
        exit(1)
    print("Container recreated with new env.")

    time.sleep(3)
    verify = subprocess.run(
        ["docker", "exec", CONTAINER_NAME, "printenv", "LOCAL_TEMP_JWT_TOKEN_FOR_MOSAIC_LAB"],
        capture_output=True, text=True
    )
    container_token = verify.stdout.strip()
    if container_token and container_token[-60:] == token[-60:]:
        print(f"VERIFIED: container has the new JWT (...{container_token[-40:]})")
    else:
        print("WARNING: container token doesn't match!")
        print(f"  Expected (last 40): ...{token[-40:]}")
        print(f"  Got      (last 40): ...{container_token[-40:]}")


def refresh_and_deploy():
    """Fetch new JWT, update .env, recreate container, verify."""
    token = refresh_jwt()
    deploy_to_docker(token)
    return token


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Refresh GAIC JWT and optionally test Mosaic API")
    parser.add_argument("--refresh", action="store_true",
                        help="Refresh JWT and update .env only (no docker deploy)")
    parser.add_argument("--test", action="store_true",
                        help="Test current JWT against Mosaic API (skip refresh)")
    parser.add_argument("--refresh-and-test", action="store_true",
                        help="Refresh JWT, update .env, then test Mosaic API (no docker deploy)")
    parser.add_argument("--refresh-deploy-test", action="store_true",
                        help="Refresh JWT, deploy to docker, then test Mosaic API")
    parser.add_argument("--check-expiry", action="store_true",
                        help="Check when current JWT expires")
    args = parser.parse_args()

    if args.check_expiry:
        jwt = read_current_jwt_from_env()
        if not jwt:
            print("No JWT found in .env")
            exit(1)
        ok = check_jwt_expiry(jwt)
        exit(0 if ok else 1)
    elif args.refresh:
        refresh_jwt()
    elif args.test:
        jwt = read_current_jwt_from_env()
        if not jwt:
            print("No JWT found in .env — run --refresh first")
            exit(1)
        ok = test_mosaic_curl(jwt)
        exit(0 if ok else 1)
    elif args.refresh_and_test:
        token = refresh_jwt()
        test_mosaic_curl(token)
    elif args.refresh_deploy_test:
        token = refresh_and_deploy()
        test_mosaic_curl(token)
    else:
        refresh_jwt()
