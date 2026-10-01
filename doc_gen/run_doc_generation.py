"""Batch-run a configurable sequence of chat turns (utterances.json) against
source files via NextWave/DARE, saving back any files the agent attaches.

Generalized document-generation runner for PPT, PDF, and DOCX output.
The turn sequence (how many turns, what each one asks, which one carries the
source-file attachment, which ones are expected to return a generated file)
lives entirely in utterances.json next to this script — nothing about the
number or wording of turns is hardcoded here. Edit that file to change the
flow; no code changes needed.

Default flow (see utterances.json):
  1. "extract"      — upload the source file, ask the agent to extract key points.
  2. "generate_ppt" — on the same thread, ask for an executive-summary PPT.

For each source file, every turn in utterances.json runs in order on one
thread. Any turn whose "expect_file_attachment" is true has its returned
file(s) downloaded and saved as "<source-stem>__<turn-id>.<ext>" (or, for a
single-turn run where you just want the plain source-stem name, see
--output-name).

Every run writes into its own fresh timestamped folder under
the output root (e.g. .../20260915_150300/) by default — no
mixing between runs, and no cross-run resume (a new run always processes its
full file list, or --limit N of it). Pass --output-dir to force a fixed,
reused folder instead; only then does the "skip already-done files" behavior
apply (or use --overwrite to force regeneration into that fixed folder).

Usage:
    # Full batch, default utterances.json, fresh timestamped output folder
    python run_doc_generation.py

    # Only the first 50 pending files (still its own timestamped folder)
    python run_doc_generation.py --limit 50

    # One specific file, end to end
    python run_doc_generation.py --file "/path/to/one.md"

    # Custom utterances / source dir
    python run_doc_generation.py --utterances-file my_utterances.json \\
        --source-dir ...

    # Force a fixed output folder (enables resume/skip across runs)
    python run_doc_generation.py --output-dir /path/to/fixed/folder

    # With a fixed folder, ignore existing outputs and regenerate
    python run_doc_generation.py --output-dir /path/to/fixed/folder --overwrite

    python run_doc_generation.py --source-dir datasets/sources --output-dir datasets/generated --pattern "*.pdf"
    """

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nextwave import NextWaveClient, NextWaveConfig  # noqa: E402
from nextwave.models import NextWaveError  # noqa: E402
from nextwave.parsing import extract_file_attachments  # noqa: E402

DEFAULT_SOURCE_DIR = os.getenv("DOC_GEN_SOURCE_DIR", os.path.join(os.path.dirname(__file__), "datasets", "sources"))
# Each run gets its own timestamped subfolder here (e.g. .../20260915_150300/) so
# outputs from different runs never mix. No cross-run resumability by design —
# every run processes its full file list (or --limit N) fresh into its own folder.
DEFAULT_OUTPUT_ROOT = os.getenv("DOC_GEN_OUTPUT_DIR", os.path.join(os.path.dirname(__file__), "datasets", "generated"))
DEFAULT_UTTERANCES_FILE = os.path.join(os.path.dirname(__file__), "utterances.json")

# Instance + credentials come from the environment -- nothing secret lives in
# the repo. Set these before running:
#   DOC_GEN_INSTANCE            e.g. "nwdemo"
#   DOC_GEN_USERNAME             e.g. "otto.eval"
#   DOC_GEN_PASSWORD             required -- no default
#   DOC_GEN_DEPLOYMENT_DOC_ID   deployment doc id for the agent
#   DOC_GEN_USERNAME_POOL       comma-separated accounts for --workers > 1
INSTANCE = os.getenv("DOC_GEN_INSTANCE", "nwdemo")
USERNAME = os.getenv("DOC_GEN_USERNAME", "otto.eval")
PASSWORD = os.getenv("DOC_GEN_PASSWORD", "")
DEPLOYMENT_DOC_ID = os.getenv("DOC_GEN_DEPLOYMENT_DOC_ID", "")
VERIFY_SSL = os.getenv("DOC_GEN_VERIFY_SSL", "false").lower() == "true"


# Pool of accounts for --workers > 1. The backend serializes conversation
# creation per account/session (server error 100-109-1000, "Could not acquire
# session lock"), so N parallel workers sharing one login collide with each
# other. Giving each worker a distinct account sidesteps that -- each gets its
# own session_id and therefore its own lock. Same password on every account
# in this test pool.
USERNAME_POOL = [u.strip() for u in os.getenv(
    "DOC_GEN_USERNAME_POOL",
    "otto.eval,otto.eval1,otto.eval2,otto.eval3,otto.eval4,otto.eval5,otto.eval6",
).split(",") if u.strip()]


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_turns(utterances_file: str) -> list[dict[str, Any]]:
    """Load the ordered turn list from utterances.json. Supports any number
    of turns (N, not hardcoded) — each is a dict with at minimum
    "id" and "utterance". Optional per-turn keys:
      attach_source_file    (bool, default False) — attach the source file on this turn.
      expect_file_attachment (bool, default False) — download any file the agent returns.
      save_as               (str, optional label used in the saved filename).
    """
    with open(utterances_file) as f:
        data = json.load(f)
    turns = data.get("turns")
    if not isinstance(turns, list) or not turns:
        raise ValueError(f"{utterances_file} must contain a non-empty 'turns' array")
    for i, t in enumerate(turns):
        if "utterance" not in t or not t["utterance"].strip():
            raise ValueError(f"turns[{i}] is missing a non-empty 'utterance'")
        t.setdefault("id", f"turn{i + 1}")
        t.setdefault("attach_source_file", False)
        t.setdefault("expect_file_attachment", False)
    return turns

#snc_host=f"{INSTANCE}.servicenowlab.com"
def connect(username: str = USERNAME, password: str = PASSWORD) -> NextWaveClient:
    missing = [n for n, v in [
        ("DOC_GEN_PASSWORD", password),
        ("DOC_GEN_DEPLOYMENT_DOC_ID", DEPLOYMENT_DOC_ID),
    ] if not v]
    if missing:
        raise RuntimeError(f"missing env vars: {', '.join(missing)}")
    cfg = NextWaveConfig(
        snc_host=f"{INSTANCE}.service-now.com",
        instance_name=INSTANCE,
        username=username,
        password=password,
        deployment_doc_id=DEPLOYMENT_DOC_ID,
        verify_ssl=False,
        stream_timeout=1200,
        idle_timeout=360,
    )
    client = NextWaveClient(cfg, on_event=_on_event)
    client.connect()
    log(f"Connected as {username}. session={client.session_id[:12]}... user={client.user_id[:12]}...")
    return client


def _is_stale_session_error(exc: Exception) -> bool:
    """True for the specific handshake failure seen when the OAuth access_token
    (fetched once at connect(), never refreshed anywhere in nextwave/auth.py) has
    gone stale mid-batch: threads.create still returns HTTP 200, but the SSE body
    is an error event ("An unexpected error occurred during handshake.", code
    100-102-1000) instead of a real thread id, so ChatkitClient.create_thread()
    correctly raises NextWaveError rather than silently continuing. Caught here so
    the caller can reconnect (fresh login) and retry, instead of every remaining
    file in the batch failing the same way for the rest of the run."""
    text = str(exc).lower()
    return isinstance(exc, NextWaveError) and ("handshake" in text or "100-102-1000" in text)


def _on_event(level: str, channel: str, message: str, data: dict | None = None) -> None:
    if level == "error":
        log(f"  !! [{channel}] {message} {data or ''}")


def fetch_attachment_bytes(client: NextWaveClient, download_url: str) -> bytes:
    resp = client.requests_session.get(
        download_url,
        headers={"X-UserToken": client.g_ck},
        timeout=(10, 60),
    )
    resp.raise_for_status()
    return resp.content


def _output_path_for(output_dir: Path, source_stem: str, turn: dict[str, Any], multi_file_turn: bool,
                      worker_tag: str | None = None) -> Path:
    """Name the saved file. If there's only one file-returning turn in the
    whole sequence, save it as "<source_stem>.<ext>" (matches the original
    single-PPT behavior exactly). If there are several (e.g. PPT + code),
    disambiguate with the turn's id/save_as: "<source_stem>__<label>.<ext>".

    worker_tag (e.g. the NextWave account that generated it, "otto.eval3")
    is appended as a further "__<tag>" suffix so, when running with multiple
    concurrent workers/accounts, the filename itself records which worker/
    account produced that file -- useful for tracing a bad output back to
    the account/session that generated it."""
    label = turn.get("save_as") or turn["id"]
    stem = f"{source_stem}__{label}" if multi_file_turn else source_stem
    if worker_tag:
        # Path.with_suffix() (called on the returned path, once the real
        # extension is known) treats everything after the LAST dot as the
        # suffix to replace -- an account name like "otto.eval3" would have
        # its ".eval3" eaten and replaced by the real extension, silently
        # truncating the tag to "otto". Strip dots from the tag so it can't
        # be mistaken for a suffix.
        stem = f"{stem}__{worker_tag.replace('.', '_')}"
    return output_dir / stem


def _existing_outputs(output_dir: Path, source_stem: str, turn: dict[str, Any], multi_file_turn: bool) -> list[Path]:
    # Deliberately ignores worker_tag: "already done" must match regardless of
    # which worker/account produced the existing file, otherwise re-running
    # with a different --workers count would never detect prior output and
    # would regenerate everything.
    base = _output_path_for(output_dir, source_stem, turn, multi_file_turn)
    return list(base.parent.glob(base.name + "*.*")) if base.parent.exists() else []


def already_done(output_dir: Path, source_stem: str, turns: list[dict[str, Any]]) -> bool:
    file_turns = [t for t in turns if t["expect_file_attachment"]]
    if not file_turns:
        return False
    multi = len(file_turns) > 1
    return all(_existing_outputs(output_dir, source_stem, t, multi) for t in file_turns)


def process_one(
    client: NextWaveClient,
    source_path: Path,
    output_dir: Path,
    turns: list[dict[str, Any]],
    worker_tag: str | None = None,
) -> bool:
    log(f"=== {source_path.name} ===")
    file_turns = [t for t in turns if t["expect_file_attachment"]]
    multi_file_turn = len(file_turns) > 1

    thread_id: str | None = None
    ok_overall = True

    for i, turn in enumerate(turns, 1):
        label = turn.get("label") or turn["id"]
        log(f"  Turn {i}/{len(turns)} [{turn['id']}]: {label}...")

        attach = [str(source_path)] if turn["attach_source_file"] else None
        result = client.send_message(turn["utterance"], thread_id=thread_id, attachment_files=attach)
        if not result.ok:
            log(f"  FAILED turn '{turn['id']}': {result.error}")
            return False
        thread_id = result.thread_id
        log(f"    reply: {len(result.response_text)} chars, thread={thread_id[:12]}...")
        if len(result.response_text) < 200:
            debug_dir = output_dir / "debug"
            debug_dir.mkdir(exist_ok=True)
            short_debug_path = debug_dir / f"{source_path.stem}__{turn['id']}__short_reply_debug.json"
            short_debug_path.write_text(json.dumps(result.events, indent=2, default=str))
            log(f"    reply unusually short ({len(result.response_text)} chars) -- dumped raw events to {short_debug_path}")
            log(f"    full reply text: {result.response_text!r}")

        if not turn["expect_file_attachment"]:
            continue

        files = extract_file_attachments(result.events)
        # "code" (share_code) is bonus material, not the deliverable -- if it
        # comes back empty (guardian block, agent has nothing to share, etc.)
        # that must never fail the whole file or touch the PPTX already saved
        # by generate_ppt. Only a turn that isn't "code" failing to attach
        # anything is a real failure (e.g. generate_ppt itself).
        is_noncritical = (turn.get("save_as") or "").lower() == "code"
        if not files:
            if is_noncritical:
                log(f"  turn '{turn['id']}': no attachment returned -- non-critical, skipping (not failing the file)")
                continue
            log(f"  FAILED: turn '{turn['id']}' expected a file attachment but got none")
            log(f"    response_text: {result.response_text[:300]}")
            debug_dir = output_dir / "debug"
            debug_dir.mkdir(exist_ok=True)
            debug_path = debug_dir / f"{source_path.stem}__{turn['id']}__raw_events_debug.json"
            debug_path.write_text(json.dumps(result.events, indent=2, default=str))
            log(f"    dumped raw events to {debug_path}")
            try:
                trace = client.fetch_trace(thread_id)
                trace_path = debug_dir / f"{source_path.stem}__{turn['id']}__aia_trace_debug.json"
                trace_path.write_text(json.dumps(trace, indent=2, default=str))
                log(f"    dumped AIA trace to {trace_path}")
            except Exception as e:
                log(f"    could not fetch AIA trace: {e}")
            ok_overall = False
            continue

        wanted_ext = (turn.get("save_as") or "").lower()
        # Extensions that can NEVER be a valid match for this save_as label --
        # e.g. a "code" turn must never accept a .pptx/.ppt, even as a
        # fallback. Without this, a turn whose real attachment doesn't match
        # wanted_ext (agent confusion, guardian-blocked prior turn, etc.) used
        # to silently fall back to files[0], which could be the DECK from an
        # earlier turn -- saving it under the wrong turn's filename/label.
        _rejected_exts = {"code": {".pptx", ".ppt"}}.get(wanted_ext, set())
        candidates = [f for f in files if f.get("title", "").lower().rsplit(".", 1)[-1]
                      not in {e.lstrip(".") for e in _rejected_exts}]
        if wanted_ext:
            matched = [f for f in candidates if f.get("title", "").lower().endswith(f".{wanted_ext}")]
            chosen = matched[0] if matched else (candidates[0] if candidates else None)
        else:
            chosen = candidates[0] if candidates else None

        if chosen is None:
            # Every attachment on this turn was one of the rejected extensions
            # (e.g. share_code returned only a .pptx). This turn is optional
            # bonus material -- log and move on WITHOUT touching ok_overall or
            # any file already saved by an earlier turn (the PPTX from
            # generate_ppt, in particular, must never be affected by this).
            log(f"  turn '{turn['id']}': no valid attachment found (all were "
                f"{[f.get('title') for f in files]}) -- skipping this turn's save, "
                f"not failing the file")
            continue

        download_url = chosen.get("download_url") or ""
        if not download_url:
            log(f"  turn '{turn['id']}': attachment '{chosen.get('title')}' has no download_url -- skipping")
            continue

        ext = Path(chosen.get("title") or "").suffix or ".bin"
        out_path = _output_path_for(output_dir, source_path.stem, turn, multi_file_turn, worker_tag).with_suffix(ext)
        log(f"    downloading '{chosen.get('title')}' ({chosen.get('size', 0)} bytes)...")
        data = fetch_attachment_bytes(client, download_url)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data)
        log(f"    saved -> {out_path} ({len(data)} bytes)")

    return ok_overall


_thread_local = threading.local()
_worker_counter = 0
_worker_counter_lock = threading.Lock()


def _next_worker_slot() -> int:
    """Assign each worker thread a stable slot index (0, 1, 2, ...) the first
    time it asks, so it always maps to the same USERNAME_POOL account across
    reconnects."""
    global _worker_counter
    with _worker_counter_lock:
        slot = _worker_counter
        _worker_counter += 1
    return slot


def _client_for_this_thread() -> NextWaveClient:
    """One NextWaveClient per worker thread, created lazily on first use and
    reused for every file that thread processes. NextWaveClient is NOT
    thread-safe to share (one mutable SessionState + one requests.Session
    per instance) -- each worker gets its own login instead.

    Each worker's login is also a distinct account from USERNAME_POOL
    (round-robin by worker slot), since the backend serializes conversation
    creation per account/session -- workers sharing one login collide on a
    server-side session lock (error 100-109-1000)."""
    client = getattr(_thread_local, "client", None)
    if client is None:
        slot = _next_worker_slot()
        username = USERNAME_POOL[slot % len(USERNAME_POOL)]
        _thread_local.username = username
        client = connect(username=username, password=PASSWORD)
        _thread_local.client = client
    return client


def _reconnect_this_thread() -> NextWaveClient:
    """Fresh login for this worker's already-assigned account (not a new
    random pick) -- so a mid-batch reconnect doesn't drift a worker onto a
    different account than the one it started with."""
    username = getattr(_thread_local, "username", USERNAME)
    client = connect(username=username, password=PASSWORD)
    _thread_local.client = client
    return client


def process_one_with_retry(source_path: Path, output_dir: Path, turns: list[dict[str, Any]]) -> bool:
    """process_one, with the same 1-retry-on-stale-session behavior as the
    sequential path, scoped to this worker thread's own client."""
    ok = False
    for attempt in (1, 2):
        client = _client_for_this_thread()
        worker_tag = getattr(_thread_local, "username", None)
        try:
            ok = process_one(client, source_path, output_dir, turns, worker_tag=worker_tag)
            break
        except Exception as e:  # noqa: BLE001 -- keep the batch going on a per-file failure
            if attempt == 1 and _is_stale_session_error(e):
                log(f"  [{threading.current_thread().name}] Session/token appears to have expired "
                    f"({e}); reconnecting (fresh login) and retrying this file...")
                try:
                    _reconnect_this_thread()
                except Exception as reconnect_exc:
                    log(f"  [{threading.current_thread().name}] Reconnect failed: {reconnect_exc}")
                    break
                continue
            log(f"  [{threading.current_thread().name}] EXCEPTION: {e}")
            traceback.print_exc()
            break
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-dir", default=DEFAULT_SOURCE_DIR, help="Folder of source files to batch-process")
    parser.add_argument(
        "--output-dir",
        default=None,
        help=(
            "Folder to save generated files into. Default: a fresh timestamped "
            f"subfolder under {DEFAULT_OUTPUT_ROOT} (e.g. .../20260915_150300/), "
            "created new every run — no cross-run resume. Pass this to force a "
            "fixed folder instead."
        ),
    )
    parser.add_argument("--file", default=None, help="Process exactly one source file (overrides --source-dir/--pattern)")
    parser.add_argument("--utterances-file", default=DEFAULT_UTTERANCES_FILE, help="Path to the turns JSON")
    parser.add_argument("--limit", type=int, default=None, help="Process at most N files (smoke test)")
    parser.add_argument("--overwrite", action="store_true", help="Regenerate even if output(s) already exist (only relevant with a fixed --output-dir)")
    parser.add_argument("--pattern", default="*.md")
    parser.add_argument("--workers", type=int, default=1, help="Number of files to process concurrently, each on "
                                                                 "its own logged-in NextWave session/thread (default: "
                                                                 "1 = sequential, same behavior as before)")
    args = parser.parse_args()

    turns = load_turns(args.utterances_file)
    log(f"Loaded {len(turns)} turn(s) from {args.utterances_file}: {[t['id'] for t in turns]}")

    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        run_stamp = time.strftime("%Y%m%d_%H%M%S")
        output_dir = Path(DEFAULT_OUTPUT_ROOT) / run_stamp
    output_dir.mkdir(parents=True, exist_ok=True)
    log(f"Output folder for this run: {output_dir}")

    if args.file:
        source_files = [Path(args.file)]
        if not source_files[0].exists():
            log(f"File not found: {args.file}")
            return
    else:
        source_dir = Path(args.source_dir)
        source_files = sorted(source_dir.glob(args.pattern))
        if not source_files:
            log(f"No files matching {args.pattern} in {source_dir}")
            return

    # Skip-already-done only makes sense against a fixed, reused --output-dir —
    # a fresh timestamped folder starts empty every run, so nothing is ever skipped there.
    if args.output_dir and not args.overwrite:
        pending = [f for f in source_files if not already_done(output_dir, f.stem, turns)]
    else:
        pending = list(source_files)
    skipped = len(source_files) - len(pending)
    if args.limit:
        pending = pending[: args.limit]

    log(f"{len(source_files)} source file(s) found, {skipped} already done (skipped), {len(pending)} to process now.")
    if not pending:
        log("Nothing to do.")
        return

    succeeded = 0
    failed = 0

    if args.workers <= 1:
        client = connect()
        reconnects = 0
        for i, source_path in enumerate(pending, 1):
            log(f"--- [{i}/{len(pending)}] ---")
            ok = False
            for attempt in (1, 2):  # 1 retry, only for a detected stale-session error
                try:
                    ok = process_one(client, source_path, output_dir, turns)
                    break
                except Exception as e:  # noqa: BLE001 — keep the batch going on a per-file failure
                    if attempt == 1 and _is_stale_session_error(e):
                        reconnects += 1
                        log(f"  Session/token appears to have expired ({e}); "
                            f"reconnecting (fresh login) and retrying this file...")
                        try:
                            client = connect()
                        except Exception as reconnect_exc:
                            log(f"  Reconnect failed: {reconnect_exc}")
                            break
                        continue
                    log(f"  EXCEPTION: {e}")
                    traceback.print_exc()
                    break
            if ok:
                succeeded += 1
            else:
                failed += 1
        log(f"Done. succeeded={succeeded} failed={failed} skipped={skipped} reconnects={reconnects}")
        return

    # --workers > 1: each worker thread logs in on its own (its own
    # NextWaveClient / SessionState / requests.Session) and pulls files off
    # the shared `pending` queue via ThreadPoolExecutor.map's work-stealing.
    log(f"Running with {args.workers} parallel workers.")
    with ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="doc-gen") as pool:
        futures = {
            pool.submit(process_one_with_retry, source_path, output_dir, turns): source_path
            for source_path in pending
        }
        for i, future in enumerate(as_completed(futures), 1):
            source_path = futures[future]
            try:
                ok = future.result()
            except Exception as e:  # noqa: BLE001 — a worker crashed outside process_one_with_retry's own handling
                log(f"  [{source_path.name}] unhandled worker exception: {e}")
                ok = False
            log(f"--- [{i}/{len(pending)}] {source_path.name}: {'OK' if ok else 'FAILED'} ---")
            if ok:
                succeeded += 1
            else:
                failed += 1

    log(f"Done. succeeded={succeeded} failed={failed} skipped={skipped}")


if __name__ == "__main__":
    main()
