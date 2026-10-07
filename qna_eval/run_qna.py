#!/usr/bin/env python3
"""Upload a document (PDF, DOCX, image, PPTX, ...) to a ServiceNow NextWave
instance and ask a question about it.

Core automation only: connect, upload, ask, capture the answer plus the
sandbox execution trace (what code the agent ran against the file), save the
result. No judging/scoring, no eval-dataset manifests.

USAGE
    # Single file, single question
    python3 run_qna.py --file /path/to/report.pdf --question "What is the total budget?"

    # Batch: a JSON list of {"id", "file", "question"} objects
    python3 run_qna.py --cases-file cases.json --workers 4

    # Just check auth works
    python3 run_qna.py --check-session

cases.json format:
    [
      {"id": "case-1", "file": "/path/to/a.pdf", "question": "..."},
      {"id": "case-2", "file": "/path/to/b.docx", "question": "..."}
    ]

Credentials come from .env.instance (next to this script) or CLI flags/env vars:
    SNC_HOST, SNC_PROTOCOL, INSTANCE_NAME, NW_USERNAME, NW_PASSWORD,
    OAUTH_CLIENT_ID, OAUTH_REDIRECT_URI, DEPLOYMENT_DOC_ID, VERIFY_SSL
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

from nextwave import NextWaveClient, NextWaveConfig
from nextwave.aia import AiaTraceConfig

HERE = Path(__file__).resolve().parent
RESULTS_DIR = HERE / "results"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _extract_tool_runs(trace_dict: dict) -> list[dict]:
    """Pull bash/sandbox tool execution summaries out of an AIA trace -- what
    code the agent actually ran against the uploaded file."""
    def _val(obj, field_name):
        v = obj.get(field_name, "")
        if isinstance(v, dict):
            return v.get("display_value") or v.get("value") or ""
        return str(v) if v else ""

    runs = []
    idx = 0
    for task in trace_dict.get("tasks", []):
        if _val(task, "type").lower() != "tool":
            continue
        meta = task.get("metadata", {})
        meta_val = meta.get("value", meta.get("display_value", {}))
        tool_name = meta_val.get("tool_name", "") if isinstance(meta_val, dict) else ""
        if tool_name != "bash":
            continue
        inputs = meta_val.get("inputs", {})
        command = inputs.get("command", [])
        code = "\n".join(command) if isinstance(command, list) else str(command)
        out_fields = (task.get("output", {}).get("value", {}).get("result", {}).get("Output Fields", {}))
        stdout_raw = out_fields.get("output", "")
        stdout = "\n".join(stdout_raw) if isinstance(stdout_raw, list) else str(stdout_raw or "")
        stderr = str(out_fields.get("error", "") or "")
        idx += 1
        runs.append({
            "run_index": idx,
            "status": _val(task, "status"),
            "duration_ms": _val(task, "execution_time_ms"),
            "code": code,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": str(out_fields.get("exit_code", "")) or ("0" if out_fields.get("success") else ""),
        })
    return runs


def connect(cfg: NextWaveConfig, fetch_trace: bool) -> NextWaveClient:
    trace_cfg = AiaTraceConfig(fetch_genai_logs=False, poll_attempts=15, poll_interval=2.0) if fetch_trace else None
    client = NextWaveClient(cfg, trace_cfg=trace_cfg)
    client.connect()
    log(f"Connected as {cfg.username}. session={client.session_id[:12]}...")
    return client


def ask_one(client: NextWaveClient, case: dict, fetch_trace: bool, timeout: float) -> dict:
    file_str = case.get("file") or case.get("file_path")
    question = case.get("question") or case.get("test_query")
    cid = case.get("id") or Path(file_str).stem
    file_path = Path(file_str)
    log(f"[{cid}] uploading {file_path.name} and asking: {question[:80]}...")

    t0 = time.time()
    record: dict[str, Any] = {
        "id": cid, "file": str(file_path), "question": question,
        "gold_standard": case.get("gold_standard", ""),
        "expected_behaviour": case.get("expected_behaviour", ""),
        "notes": case.get("notes", ""),
        "answer": "", "error": "", "runs": [], "thread_id": "",
        "ttfb_ms": 0, "response_time_ms": 0, "elapsed_s": 0,
    }

    if not file_path.exists():
        record["error"] = f"file not found: {file_path}"
        return record

    try:
        thread_id = client.create_thread()
        record["thread_id"] = thread_id
        result = client.send_message(question, thread_id=thread_id, attachment_files=[str(file_path)])
        record["answer"] = result.response_text
        record["ttfb_ms"] = result.ttfb_ms
        record["response_time_ms"] = result.response_time_ms
        if result.error:
            record["error"] = result.error

        if fetch_trace:
            trace = client.fetch_trace(thread_id)
            record["runs"] = _extract_tool_runs(trace.to_dict())

        log(f"[{cid}] answer: {len(record['answer'])} chars | sandbox runs: {len(record['runs'])}")
    except Exception as e:  # noqa: BLE001 -- keep the batch going on a per-case failure
        record["error"] = f"{type(e).__name__}: {e}"
        log(f"[{cid}] ERROR: {record['error']}")

    record["elapsed_s"] = round(time.time() - t0, 1)
    return record


def main() -> None:
    env_file = HERE / ".env.instance"
    load_dotenv(env_file if env_file.exists() else None)

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default="", help="Single file to upload (pdf/docx/image/pptx/...)")
    ap.add_argument("--question", default="", help="Question to ask about --file")
    ap.add_argument("--cases-file", default="", help="JSON list of {id, file, question} for batch mode")
    ap.add_argument("--workers", type=int, default=1, help="Concurrent cases, each on its own logged-in session")
    ap.add_argument("--timeout", type=float, default=1200.0, help="SSE stream timeout (seconds)")
    ap.add_argument("--no-trace", action="store_true", help="Skip AIA sandbox-trace fetch (faster)")
    ap.add_argument("--check-session", action="store_true", help="Test auth and exit")
    ap.add_argument("--output", default="", help="Path to save result JSON (default: results/<timestamp>.json)")
    ap.add_argument("--resume", action="store_true", help="If --output file already exists, skip cases whose id already succeeded in it and only run the rest")

    ap.add_argument("--host", default=os.getenv("SNC_HOST", ""))
    ap.add_argument("--protocol", default=os.getenv("SNC_PROTOCOL", "https"))
    ap.add_argument("--instance-name", default=os.getenv("INSTANCE_NAME", ""))
    ap.add_argument("--username", default=os.getenv("NW_USERNAME", ""))
    ap.add_argument("--password", default=os.getenv("NW_PASSWORD", ""))
    ap.add_argument(
        "--username-pool", default=os.getenv(
            "NW_USERNAME_POOL",
            "otto.eval,otto.eval1,otto.eval2,otto.eval3,otto.eval4,otto.eval5,otto.eval6",
        ),
        help="Comma-separated accounts (same password as --username) for --workers > 1. "
             "The backend serializes conversation creation per account/session, so parallel "
             "workers sharing one login collide on a server-side session lock; giving each "
             "worker a distinct account from this pool (round-robin by worker slot) avoids that. "
             "Defaults to the shared nwdemo test pool (otto.eval + otto.eval1..6); override with "
             "--username-pool or NW_USERNAME_POOL for a different instance.",
    )
    ap.add_argument("--oauth-client-id", default=os.getenv("OAUTH_CLIENT_ID", "46e42d08770746f1802167828fcc6132"))
    ap.add_argument("--oauth-redirect-uri", default=os.getenv("OAUTH_REDIRECT_URI", "/api/snc/aiexauth/oauth/authorize"))
    ap.add_argument("--deployment-doc-id", default=os.getenv("DEPLOYMENT_DOC_ID", ""))
    ap.add_argument("--verify-ssl", default=os.getenv("VERIFY_SSL", "false"))
    args = ap.parse_args()

    if not args.host or not args.username or not args.password:
        raise SystemExit("--host/--username/--password (or SNC_HOST/NW_USERNAME/NW_PASSWORD) are required")

    instance_name = args.instance_name or args.host.split(".")[0]
    fetch_trace = not args.no_trace

    cfg = NextWaveConfig(
        snc_host=args.host,
        snc_protocol=args.protocol,
        instance_name=instance_name,
        username=args.username,
        password=args.password,
        oauth_client_id=args.oauth_client_id,
        oauth_redirect_uri=args.oauth_redirect_uri,
        deployment_doc_id=args.deployment_doc_id,
        verify_ssl=args.verify_ssl.lower() in ("1", "true", "yes"),
        stream_timeout=int(args.timeout),
        verbose=True,
    )

    if args.check_session:
        client = connect(cfg, fetch_trace=False)
        print(f"SESSION ALIVE. session={client.session_id} user={client.user_id}")
        return

    # Build the case list
    if args.cases_file:
        cases = json.loads(Path(args.cases_file).read_text())
    elif args.file and args.question:
        cases = [{"id": Path(args.file).stem, "file": args.file, "question": args.question}]
    else:
        raise SystemExit("Provide either --file + --question, or --cases-file")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.output) if args.output else RESULTS_DIR / f"qna_{time.strftime('%Y%m%d_%H%M%S')}.json"

    results: list[dict] = []
    if args.resume and out_path.exists():
        results = json.loads(out_path.read_text())
        done_ids = {r["id"] for r in results if not r.get("error")}
        before = len(cases)
        cases = [c for c in cases if (c.get("id") or Path(c.get("file") or c.get("file_path")).stem) not in done_ids]
        log(f"--resume: {len(done_ids)} case(s) already done in {out_path}, {before - len(cases)} skipped, {len(cases)} remaining")

    write_lock = threading.Lock()

    def _save() -> None:
        with write_lock:
            out_path.write_text(json.dumps(results, indent=2))

    if args.workers <= 1:
        client = connect(cfg, fetch_trace)
        for case in cases:
            results.append(ask_one(client, case, fetch_trace, args.timeout))
            _save()
    else:
        pool = [u.strip() for u in args.username_pool.split(",") if u.strip()] or [args.username]
        if len(pool) < args.workers:
            log(f"WARNING: --username-pool has only {len(pool)} account(s) for {args.workers} workers -- "
                f"workers sharing an account WILL collide on the server-side session lock (error 100-109-1000). "
                f"Pass --username-pool with >= --workers distinct accounts to avoid this.")
        log(f"Running {len(cases)} case(s) with {args.workers} parallel worker(s), "
            f"accounts: {[pool[i % len(pool)] for i in range(args.workers)]}")

        # Pre-authenticate all workers SEQUENTIALLY first. Logging in from the
        # same account concurrently can invalidate each other's OAuth auth
        # codes (seen as "oauth_token.do failed: 401 access_denied"), so each
        # worker's one-time login happens before any of them start sending
        # messages, never interleaved with another worker's login. Each
        # worker also gets its own account (round-robin over --username-pool)
        # since the backend serializes conversation creation per account/
        # session -- workers sharing one login collide on that lock too.
        worker_clients: list[NextWaveClient] = []
        for wi in range(args.workers):
            worker_username = pool[wi % len(pool)]
            worker_cfg = dataclasses.replace(cfg, username=worker_username)
            worker_clients.append(connect(worker_cfg, fetch_trace))
            log(f"  worker {wi + 1}/{args.workers} authenticated as {worker_username}")

        _thread_local = threading.local()
        _next_slot = iter(range(args.workers))
        _slot_lock = threading.Lock()

        def _client_for_thread() -> NextWaveClient:
            c = getattr(_thread_local, "client", None)
            if c is None:
                with _slot_lock:
                    slot = next(_next_slot)
                c = worker_clients[slot]
                _thread_local.client = c
            return c

        def _run(case: dict) -> dict:
            # Never let an exception here (e.g. a dead session) escape and take
            # down the whole batch -- one bad case must not lose every other
            # result that already succeeded.
            try:
                return ask_one(_client_for_thread(), case, fetch_trace, args.timeout)
            except Exception as e:  # noqa: BLE001
                file_str = case.get("file") or case.get("file_path", "")
                cid = case.get("id") or Path(file_str).stem
                log(f"[{cid}] WORKER ERROR: {type(e).__name__}: {e}")
                return {
                    "id": cid, "file": file_str,
                    "question": case.get("question") or case.get("test_query", ""),
                    "gold_standard": case.get("gold_standard", ""),
                    "expected_behaviour": case.get("expected_behaviour", ""),
                    "notes": case.get("notes", ""),
                    "answer": "", "error": f"{type(e).__name__}: {e}", "runs": [], "thread_id": "",
                    "ttfb_ms": 0, "response_time_ms": 0, "elapsed_s": 0,
                }

        with ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="qna") as pool:
            futures = {pool.submit(_run, c): c for c in cases}
            for fut in as_completed(futures):
                results.append(fut.result())
                _save()

    ok = sum(1 for r in results if not r["error"])
    log(f"Done. {ok}/{len(results)} succeeded. Results -> {out_path}")


if __name__ == "__main__":
    main()
