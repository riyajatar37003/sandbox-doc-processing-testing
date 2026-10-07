#!/usr/bin/env python3
"""Standalone full-stack sandbox doc-QA eval runner (ONE category first).

End-to-end, per case:
  1. create a ChatKit thread            POST /api/chatkit            {type: threads.create}
  2. upload the document                POST /api/chatkit/upload     (multipart)
  3. send the test query (SSE)          POST /api/chatkit            {type: threads.add_user_message}
  4. capture the model's FINAL answer from the SSE stream
  5. pull every sandbox script + output the agent ran, from the AO-v2 container
     logs ([SANDBOX_EVAL_CAPTURE] lines, keyed by conversationId)
  6. write everything to an .xlsx (no scoring — you judge later)

NO LLM JUDGE. We only dump: query, gold standard, model final answer, and each
sandbox script + its output.

------------------------------------------------------------------------------
PREREQUISITES
------------------------------------------------------------------------------
1. Full stack running (conversation-server + agent-orchestrator-v2 + cache + valkey).
2. AO-v2 container started WITH eval capture enabled so the bash tool emits
   [SANDBOX_EVAL_CAPTURE] log lines:
       in conversation-server/docker-compose.yaml, under agent-orchestrator-v2:
         environment:
           SANDBOX_EVAL_CAPTURE: "true"
       then: docker compose up -d agent-orchestrator-v2
   (bash_tool.py is volume-mounted/live-reloaded; no image rebuild needed.)
3. A valid bearer token for the running UI session. Easiest: open the local VA
   UI, copy the `Authorization: Bearer ...` header from any /api/chatkit request
   in browser devtools. Pass it via --token or BEARER_TOKEN env.
4. Run dataset_loader.py first to produce cases.json.

USAGE
    BEARER_TOKEN=... python3 run_full_stack_eval.py --category office --limit 1
    python3 run_full_stack_eval.py --category office --limit 5 --token "Bearer ..."

Raw SSE for each case is dumped to ./debug/<case_id>.sse for first-run tuning of
the final-answer extraction (event schema may need a tweak — see extract_final_answer).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import escape
from pathlib import Path

import httpx

from trace_request import build_trace

AGENT_OUTPUT_ROOT = Path(__file__).parent / "results" / "agent_output_runs"
INVOICE_HEADER_FIELDS = [
    "supplier_invoice_number", "supplier", "supplier_tax_id", "invoice_date",
    "purchase_order", "supplier_bank_name", "account_number", "ach_routing_number",
    "swift_code", "wire_routing_number", "tax_rate", "invoice_amount", "subtotal",
    "shipping", "other_charges", "tax_amount", "invoice_currency",
    "bill_to_company_name", "bill_to_street", "bill_to_city", "bill_to_state_or_province",
    "bill_to_zip_or_postal_code", "bill_to_country",
    "remit_address", "remit_to_city", "remit_to_state_or_province",
    "remit_to_zip_or_postal_code", "remit_to_country",
    "ship_to_street", "ship_to_city", "ship_to_state_or_province",
    "ship_to_zip_or_postal_code", "ship_to_country",
    "original_invoice_number",
]
INVOICE_LINE_FIELDS = ["line_description", "line_quantity", "line_unit_price", "tax_amount", "line_amount"]
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

HERE = Path(__file__).parent
PROJECT_ROOT = HERE
SKILLS_CORE_DIR = Path(
    "/Users/riyaj.atar/Library/CloudStorage/OneDrive-ServiceNow/offglide-services/agent-orchestrator-v2"
    "/va_agentic/native_agent/execution/pipeline_stages/dare/skills/core"
)
FILE_SKILLS = ["doc-qna", "read-csv", "read-docx", "read-image", "read-pdf", "read-pptx", "read-xlsx"]
CASES_JSON = HERE / "cases.json"
DEBUG_DIR = HERE / "debug"

AO_V2_CONTAINER = "conversation-server-agent-orchestrator-v2-1"
CAPTURE_MARKER = "[SANDBOX_EVAL_CAPTURE]"

MIME = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".csv": "text/csv",
    ".pdf": "application/pdf",
}


# --------------------------------------------------------------------------- #
# ChatKit client — replicates the local VA UI (ChatKitConnector) exactly
# --------------------------------------------------------------------------- #
class ChatKitClient:
    def __init__(self, base_url: str, session: dict, timeout: float, session_file: Path | None = None):
        self.base = base_url.rstrip("/")
        self.session = session  # captured metadata block (session.json)
        self.token = f"Bearer {session.get('authToken', 'test-token')}"
        self.device_id = session.get("customContext", {}).get("deviceId", str(uuid.uuid4()))
        self._timeout = timeout
        self._local = threading.local()
        self.session_file = session_file

    def reload_session_from_disk(self) -> None:
        """Re-read session_file into this client — used after init_session.py re-captures it."""
        if not self.session_file:
            raise RuntimeError("ChatKitClient has no session_file to reload from")
        self.session = json.loads(self.session_file.read_text())
        self.token = f"Bearer {self.session.get('authToken', 'test-token')}"
        self.device_id = self.session.get("customContext", {}).get("deviceId", self.device_id)

    @property
    def client(self) -> httpx.Client:
        """Thread-local httpx.Client — httpx.Client is not thread-safe."""
        if not hasattr(self._local, "client"):
            self._local.client = httpx.Client(timeout=self._timeout)
        return self._local.client

    def _headers(self, extra: dict | None = None) -> dict:
        h = {
            "Authorization": self.token,
            "X-Session-Id": self.session["sessionId"],
            "Cookie": f"sn-chatbot-deviceId={self.device_id}",
            "Origin": "http://localhost:8060",
        }
        if extra:
            h.update(extra)
        return h

    def _metadata(self, conversation_id: str) -> dict:
        """Full metadata block the UI sends on every request, with this conversation."""
        m = dict(self.session)
        m["conversationId"] = conversation_id
        m["requestId"] = str(uuid.uuid4())
        return m

    def check_session(self) -> tuple[bool, str]:
        """Lightweight threads.list ping -> (alive, detail). Mirrors what the UI polls."""
        body = {"type": "threads.list", "params": {"order": "desc", "limit": 1},
                "metadata": self._metadata(self.session.get("conversationId", ""))}
        try:
            r = self.client.post(
                f"{self.base}/api/chatkit",
                headers=self._headers({"Content-Type": "application/json", "Accept": "application/json"}),
                json=body,
            )
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"
        return (r.status_code == 200), f"HTTP {r.status_code} {r.text[:160]}"

    def create_thread(self) -> str:
        """threads.create -> new conversationId."""
        body = {
            "type": "threads.create",
            "params": {"input": None, "initiateLiveAgent": False},
            "metadata": self._metadata(""),
        }
        r = self.client.post(
            f"{self.base}/api/chatkit",
            headers=self._headers({"Content-Type": "application/json", "Accept": "application/json"}),
            json=body,
        )
        r.raise_for_status()
        cid = _find_first(r.json(), ("conversationId", "thread_id", "threadId", "id"))
        if not cid:
            raise RuntimeError(f"No conversationId in threads.create response: {r.text[:400]}")
        return cid

    def reset_reusable(self, conversation_id: str) -> None:
        """Send a lightweight request that resets reUsableConversationId.

        ReUsableConversationService clears the reusable pointer when it sees
        any USER_INTERACTION_TYPES request. We send items.feedback with a
        dummy item — non-streaming, no side effects, just enough for the
        resetReusableConversationIdIfApplicable check to fire."""
        body = {
            "type": "items.feedback",
            "params": {"item_id": "dummy", "feedback": "thumbs_up"},
            "metadata": self._metadata(conversation_id),
        }
        try:
            r = self.client.post(
                f"{self.base}/api/chatkit",
                headers=self._headers({"Content-Type": "application/json", "Accept": "application/json"}),
                json=body,
                timeout=5.0,
            )
        except Exception:  # noqa: BLE001
            pass  # best-effort; the conversation is already created

    def upload(self, conversation_id: str, file_path: Path) -> dict:
        """POST /api/chatkit/upload -> attachment dict.

        Mirrors chatKitConnector.ts's uploadAttachment(): the UI only calls
        dmsupload when isDmsAvailable is true (DMS document conversion via
        createDocument); otherwise it uses this plain endpoint, which skips
        DMS entirely. Hardcoding dmsupload here was hitting createDocument
        unconditionally and 500ing whenever DMS isn't set up for the session."""
        mime = MIME.get(file_path.suffix.lower(), "application/octet-stream")
        with file_path.open("rb") as fh:
            files = {"data": (file_path.name, fh, mime)}
            params = {
                "conversationId": conversation_id,
                "sessionId": self.session["sessionId"],
                "name": file_path.name,
                "mimeType": mime,
            }
            r = self.client.post(
                f"{self.base}/api/chatkit/upload",
                headers=self._headers(),
                params=params,
                files=files,
            )
        r.raise_for_status()
        data = r.json()
        if not data.get("id"):
            raise RuntimeError(f"No attachment id in upload response: {r.text[:400]}")
        return data

    def send_message_sse(self, conversation_id: str, attachments: list[dict], text: str, raw_dump: Path) -> str:
        """threads.add_user_message over SSE -> final assistant answer text.

        Accepts one or more attachments (multiple for cross-file cases — all uploaded
        into the same conversation and referenced together in a single message)."""
        body = {
            "type": "threads.add_user_message",
            "params": {
                "thread_id": conversation_id,
                "input": {
                    "content": [{"type": "input_text", "text": text}],
                    # Pass the full raw upload() response through untouched -- mirrors
                    # nextwave/chatkit.py's send_message(), which does the same. Trimming
                    # this to 4 keys and injecting a "document_id": None that doesn't
                    # exist on the real response broke sandbox file-staging (the agent
                    # could no longer read the attachment at all, even though upload
                    # itself still succeeded) -- confirmed via local-eval/doc_gen testing.
                    "attachments": attachments,
                    "quoted_text": None,
                    "inference_options": {},
                },
            },
            "metadata": self._metadata(conversation_id),
        }
        events: list[dict] = []
        # A heavy multi-page invoice (OCR + per-page vision + bash crops) can go well over 90s
        # between thread.item.added workflow-status pushes while the server is still working —
        # confirmed against extraction-1293 (11-page scanned invoice): the trace log showed the
        # agent still issuing vision/bash calls at ~450s when a 90s idle_timeout cut the stream,
        # producing a false "final answer: 0 chars" even though every sandbox run succeeded.
        idle_timeout = 600  # seconds of only heartbeats after first assistant event
        initial_timeout = 300  # max seconds to wait for the first assistant event
        with self.client.stream(
            "POST",
            f"{self.base}/api/chatkit",
            headers=self._headers({"Content-Type": "application/json", "Accept": "text/event-stream"}),
            json=body,
        ) as resp:
            resp.raise_for_status()
            raw_lines = []
            stream_start = time.time()
            last_data_time = stream_start
            has_assistant_event = False
            for line in resp.iter_lines():
                if not line:
                    continue
                raw_lines.append(line)
                if line.startswith("data:"):
                    last_data_time = time.time()
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    if payload:
                        try:
                            ev = json.loads(payload)
                            events.append(ev)
                            # Track when the bot actually starts responding
                            etype = ev.get("type", "")
                            item_type = ev.get("item", {}).get("type", "") if isinstance(ev.get("item"), dict) else ""
                            if item_type not in ("user_message", "input_text", "") or "text_delta" in etype:
                                has_assistant_event = True
                            # thread.done signals the stream is complete
                            if etype == "thread.done":
                                break
                        except json.JSONDecodeError:
                            pass
                else:
                    # Heartbeat or comment — apply idle timeout after first
                    # assistant event, or initial_timeout if agent never responded.
                    if has_assistant_event and (time.time() - last_data_time) > idle_timeout:
                        break
                    if not has_assistant_event and (time.time() - stream_start) > initial_timeout:
                        break
            ts = time.strftime("%Y%m%d_%H%M%S")
            stamped_dump = raw_dump.with_stem(f"{raw_dump.stem}_{ts}")
            with stamped_dump.open("w") as f:
                f.write("\n".join(raw_lines) + "\n")
        return extract_final_answer(events)


def _find_first(obj, keys):
    """Depth-first search for the first matching key in a nested dict/list."""
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and isinstance(obj[k], (str, int)):
                return str(obj[k])
        for v in obj.values():
            r = _find_first(v, keys)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_first(v, keys)
            if r:
                return r
    return None


def _content_text(content) -> str:
    """Join output_text parts of an assistant_message item's content list."""
    if not isinstance(content, list):
        return ""
    return "".join(
        p.get("text", "") for p in content
        if isinstance(p, dict) and p.get("type") == "output_text" and isinstance(p.get("text"), str)
    )


_TRANSITION_MSG = re.compile(r"^\s*(command executed successfully|command failed|command complete)\b", re.I)


def _extract_text_from_item(item: dict) -> str:
    """Pull text from any item type — assistant_message, text, widget, etc."""
    if not isinstance(item, dict):
        return ""
    itype = item.get("type", "")

    # Skip user-originated items — these echo the query, not the answer
    if itype in ("user_message", "input_text", "input_message"):
        return ""

    # assistant_message → content[].output_text.text
    if itype == "assistant_message":
        return _content_text(item.get("content"))

    # Generic content list (covers transition_message, text items, etc.)
    content = item.get("content")
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                t = c.get("text", "")
                if isinstance(t, str) and t.strip():
                    parts.append(t)
        if parts:
            return "".join(parts)
    if isinstance(content, str) and content.strip():
        return content

    # Plain text field on the item itself
    if isinstance(item.get("text"), str) and item["text"].strip():
        return item["text"]

    # Widget / rich items: look for body.text or data.text
    for key in ("body", "data"):
        sub = item.get(key)
        if isinstance(sub, dict) and isinstance(sub.get("text"), str) and sub["text"].strip():
            return sub["text"]

    return ""


def extract_final_answer(events: list[dict]) -> str:
    """Reconstruct the assistant's final answer from ChatKit thread.item.* events.

    Captures text from assistant_message items (snapshot or streamed deltas) as
    well as any other item type that carries text (transition_message from
    show_text_output, widget items, etc.). Returns the longest non-transition
    text found.
    """
    snapshot: dict[str, str] = {}   # item_id -> full text from added/done
    deltas: dict[str, list[str]] = {}  # item_id -> streamed delta fragments

    for ev in events:
        etype = ev.get("type", "")
        if etype in ("thread.item.added", "thread.item.done"):
            item = ev.get("item", {})
            iid = item.get("id", "")
            txt = _extract_text_from_item(item)
            if txt:
                snapshot[iid] = txt
        elif etype == "thread.item.updated":
            iid = ev.get("item_id", "")
            upd = ev.get("update", {})
            utype = str(upd.get("type", ""))
            if utype.endswith("text_delta"):
                deltas.setdefault(iid, []).append(upd.get("delta", ""))
            elif utype.endswith("content_part.added"):
                t = upd.get("content", {}).get("text", "")
                if t:
                    snapshot[iid] = t

    texts = []
    for iid in set(snapshot) | set(deltas):
        t = (snapshot.get(iid) or "".join(deltas.get(iid, []))).strip()
        # Exclude the bash-tool TRANSITION messages ("Command executed successfully" /
        # "Command failed …") — those are not the model's answer. If only transition
        # text exists, return "" (honest: the turn produced no real answer).
        if t and not _TRANSITION_MSG.match(t):
            texts.append(t)
    return max(texts, key=len, default="")


# --------------------------------------------------------------------------- #
# Sandbox capture (from AO-v2 container logs)
# --------------------------------------------------------------------------- #
PROVIDER_LOG_MSG = "[PIPELINE] LLM provider type resolved from instance config"
# DARE agent-skill mount (progressive disclosure — read-pdf, doc-qna, document-capture, etc.),
# field "skill". Distinct from the older catalog-skill mechanism below (field "skill_name"),
# which fires for structured topic skills (e.g. "Employee Badge request") that file-reading
# categories like routing/pdf/office never go through.
SKILL_MOUNT_LOG_MSG = "[DARE_SKILLS] Skill resources mounted"
SKILL_LOG_MSG = "[TOOL] skill execution invoked"


def _container_logs(since_epoch: float) -> str:
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(since_epoch - 2))
    try:
        out = subprocess.run(
            ["docker", "logs", "--since", since, AO_V2_CONTAINER],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] could not read container logs: {e}")
        return ""
    return out.stdout + out.stderr


def write_trace(conversation_id: str, since_epoch: float, out_path: Path) -> None:
    """Save the full request-lifecycle trace for this conversation next to the
    .sse dump. Reconstructs the timeline (skills loaded, tools picked, bash code
    + output, vision, LLM turns) from the AO-v2 container logs. Best-effort — a
    trace failure must never fail the eval case."""
    try:
        logs = _container_logs(since_epoch)
        trace = build_trace(
            logs.splitlines(),
            conversation_id,
            tools_only=True,
            max_blob=2000,  # bash code/stdout always full; other payloads capped
            color=False,
        )
        out_path.write_text(trace + "\n", encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] could not write trace for {conversation_id}: {e}")


def read_provider(conversation_id: str, since_epoch: float) -> dict:
    """Resolve the LLM provider for this conversation from the AO-v2 log line
    "[PIPELINE] LLM provider type resolved from instance config" (carries
    gen_ai_provider + provider_type + conversation_id)."""
    info = {"provider": "", "provider_type": ""}
    for line in _container_logs(since_epoch).splitlines():
        if PROVIDER_LOG_MSG in line and conversation_id in line:
            try:
                rec = json.loads(line[line.index("{"):])
                info["provider"] = rec.get("gen_ai_provider", info["provider"])
                info["provider_type"] = rec.get("provider_type", info["provider_type"])
            except (json.JSONDecodeError, ValueError):
                pass
    return info


def read_skills(conversation_id: str, since_epoch: float) -> list[str]:
    """Skill(s) actually loaded/invoked to answer this conversation. Unions two
    distinct AO-v2 mechanisms, in log order, de-duplicated:
      - "[DARE_SKILLS] Skill resources mounted" (field "skill") — DARE agent-skill
        progressive disclosure, e.g. read-pdf/doc-qna/document-capture routing.
      - "[TOOL] skill execution invoked" (field "skill_name") — older catalog-skill
        (topic) invocation, e.g. "Employee Badge request"."""
    skills: list[str] = []
    for line in _container_logs(since_epoch).splitlines():
        if conversation_id not in line:
            continue
        if SKILL_MOUNT_LOG_MSG not in line and SKILL_LOG_MSG not in line:
            continue
        try:
            rec = json.loads(line[line.index("{"):])
        except (json.JSONDecodeError, ValueError):
            continue
        if rec.get("conversation_id") != conversation_id:
            continue
        skill_name = rec.get("skill") or rec.get("skill_name")
        if skill_name and skill_name not in skills:
            skills.append(skill_name)
    return skills


def _extract_json_blob(text: str) -> dict | None:
    """Pull a JSON object out of a model answer that may have prose/fences around it."""
    if not text:
        return None
    text = text.strip()
    m = _JSON_FENCE_RE.search(text)
    candidates = [m.group(1)] if m else []
    candidates.append(text)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start:end + 1])
    for cand in candidates:
        try:
            return json.loads(cand.strip())
        except json.JSONDecodeError:
            continue
    return None


def _wrap_field_value(v) -> dict:
    if isinstance(v, dict) and "value" in v:
        return {"value": v.get("value", ""), "confidence": v.get("confidence", 0.0)}
    return {"value": "" if v is None else str(v), "confidence": 0.0}


def write_invoice_agent_output(case_id: str, final_answer: str, run_dir: Path) -> None:
    """For invoice_extraction cases: parse the model's JSON answer and write it
    into <run_dir>/agent_output/<stem>_agent_output.json in the same
    {field: {value, confidence}} schema as ground_truth, so invoice_eval.py can
    score it directly. run_dir is a fresh timestamped folder per eval run, so
    nothing from a prior run is ever overwritten. Best-effort — never raises
    into the caller."""
    if not case_id.startswith("extraction-") or not final_answer:
        return
    try:
        parsed = _extract_json_blob(final_answer)
        if parsed is None:
            print(f"  [{case_id}] [warn] could not parse JSON from final_answer — agent_output not written")
            return
        stem = case_id[len("extraction-"):]
        out = {f: _wrap_field_value(parsed.get(f, "")) for f in INVOICE_HEADER_FIELDS}
        lines_out = []
        for line in parsed.get("lines", []) or []:
            if isinstance(line, dict):
                lines_out.append({lf: _wrap_field_value(line.get(lf, "")) for lf in INVOICE_LINE_FIELDS})
        out["lines"] = lines_out
        out_dir = run_dir / "agent_output"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{stem}_agent_output.json"
        out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
        print(f"  [{case_id}] agent_output -> {out_path}")
    except Exception as e:  # noqa: BLE001
        print(f"  [{case_id}] [warn] failed writing agent_output: {e}")


def write_classification_output(case_id: str, final_answer: str, run_dir: Path) -> None:
    """For invoice_classification cases: parse the model's JSON answer and write it,
    unmodified, into <run_dir>/classification_output/<stem>_classification.json so
    invoice_eval.py's load_classification_index() (which globs *.json in that dir and
    matches entries by their own attachment_name field) can score it directly. One file
    per case — load_classification_index groups by attachment_name itself, so this does
    not need to be one-file-per-stem like write_invoice_agent_output. Best-effort — never
    raises into the caller."""
    if not case_id.startswith("classification-") or not final_answer:
        return
    try:
        parsed = _extract_json_blob(final_answer)
        if parsed is None or "classification_result" not in parsed:
            print(f"  [{case_id}] [warn] could not parse classification_result from final_answer — "
                  f"classification_output not written")
            return
        stem = case_id[len("classification-"):]
        out_dir = run_dir / "classification_output"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{stem}_classification.json"
        out_path.write_text(json.dumps(parsed, indent=2, ensure_ascii=False))
        print(f"  [{case_id}] classification_output -> {out_path}")
    except Exception as e:  # noqa: BLE001
        print(f"  [{case_id}] [warn] failed writing classification_output: {e}")


def failure_summary(stderr: str, exit_code) -> str:
    """One-line reason a sandbox run failed (empty for success).

    Picks the most informative line from stderr — the final `Type: message`
    exception line if present (e.g. "InvalidFileException: openpyxl does not
    support  file format"), else the last non-empty stderr line.
    """
    if str(exit_code) == "0":
        return ""
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    if not lines:
        return f"non-zero exit ({exit_code}), no stderr"
    for ln in reversed(lines):  # last "ExceptionType: message" wins
        if re.match(r"[\w.]+(Error|Exception|Warning)\b", ln):
            return ln[:300]
    return lines[-1][:300]


def read_sandbox_runs(conversation_id: str, since_epoch: float) -> list[dict]:
    """Count sandbox (bash) tool executions for this conversation.

    Primary: look for [SANDBOX_EVAL_CAPTURE] marker lines (explicit capture).
    Fallback: count tg_bash_* entries in processing-state logs from AO-v2.
    """
    runs = []
    logs = _container_logs(since_epoch)
    # --- primary: explicit capture marker ---
    for line in logs.splitlines():
        if CAPTURE_MARKER not in line or conversation_id not in line:
            continue
        m = re.search(r"\{.*\}", line)
        if not m:
            continue
        try:
            rec = json.loads(m.group())
        except json.JSONDecodeError:
            continue
        if rec.get("conversation_id") != conversation_id:
            continue
        runs.append({
            "code": rec.get("code", ""),
            "stdout": rec.get("stdout", ""),
            "stderr": rec.get("stderr", ""),
            "exit_code": rec.get("exit_code", ""),
            "duration_ms": rec.get("duration_ms", ""),
            "truncated": rec.get("truncated", False),
            "output_pct": rec.get("output_pct", ""),
            "output_bytes": rec.get("output_bytes", 0),
        })
    if runs:
        for i, r in enumerate(runs):
            if r.get("truncated"):
                ob = r.get("output_bytes", 0)
                size_kb = round(ob / 1024, 1) if ob else "?"
                print(f"  [warn] sandbox run {i+1} output TRUNCATED — original {size_kb} KB ({r.get('output_pct', '?')} of 200KB cap)")
        return runs
    # --- fallback: count tg_bash_ entries in processing-state logs ---
    bash_ids: set[str] = set()
    truncated_count = 0
    for line in logs.splitlines():
        if conversation_id not in line:
            continue
        if "tg_bash_" in line:
            for m in re.finditer(r"tg_bash_\d+", line):
                bash_ids.add(m.group())
        if "Bash output truncated" in line:
            truncated_count += 1
    for bid in sorted(bash_ids):
        runs.append({"code": "", "stdout": "", "stderr": "", "exit_code": "", "duration_ms": "", "id": bid})
    if truncated_count:
        print(f"  [warn] {truncated_count} sandbox output(s) truncated (>200KB cap)")
    return runs


# --------------------------------------------------------------------------- #
# Minimal standalone .xlsx writer (no openpyxl dependency)
# --------------------------------------------------------------------------- #
def _col(n: int) -> str:
    s = ""
    while n >= 0:
        s = chr(n % 26 + 65) + s
        n = n // 26 - 1
    return s


def _sheet_xml(rows: list[list[str]]) -> str:
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']
    for ri, row in enumerate(rows, start=1):
        out.append(f'<row r="{ri}">')
        for ci, val in enumerate(row):
            ref = f"{_col(ci)}{ri}"
            text = escape(str(val if val is not None else ""))
            out.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>')
        out.append("</row>")
    out.append("</sheetData></worksheet>")
    return "".join(out)


def write_xlsx(path: Path, sheets: list[tuple[str, list[list[str]]]]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            + "".join(
                f'<Override PartName="/xl/worksheets/sheet{i+1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                for i in range(len(sheets))) +
            '</Types>')
        z.writestr("_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>')
        sheets_xml = "".join(f'<sheet name="{escape(n)}" sheetId="{i+1}" r:id="rId{i+1}"/>' for i, (n, _) in enumerate(sheets))
        z.writestr("xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets>{sheets_xml}</sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(
                f'<Relationship Id="rId{i+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i+1}.xml"/>'
                for i in range(len(sheets))) +
            '</Relationships>')
        for i, (_, rows) in enumerate(sheets):
            z.writestr(f"xl/worksheets/sheet{i+1}.xml", _sheet_xml(rows))


# --------------------------------------------------------------------------- #
# Incremental persistence: append each case to a .jsonl sidecar and rebuild the
# xlsx after every case, so a crash/kill never loses completed work and a re-run
# resumes (skips already-done case ids).
# --------------------------------------------------------------------------- #
RESPONSES_HEADER = ["id", "category", "file", "query_type", "test_query",
                    "gold_standard", "final_answer", "provider", "provider_type", "skills_used",
                    "num_runs", "all_runs_passed", "error", "qa_latency_s"]
RUNS_HEADER = ["id", "run_index", "code", "stdout", "stderr", "exit_code", "duration_ms", "failure_summary"]


def _load_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _records_to_sheets(records: list[dict]) -> list[tuple[str, list[list[str]]]]:
    responses = [RESPONSES_HEADER]
    runs = [RUNS_HEADER]
    for r in records:
        responses.append([r.get("id", ""), r.get("category", ""), r.get("file", ""), r.get("query_type", ""),
                          r.get("test_query", ""), r.get("gold_standard", ""), r.get("final_answer", ""),
                          r.get("provider", ""), r.get("provider_type", ""), ", ".join(r.get("skills_used", [])),
                          str(r.get("num_runs", 0)),
                          str(r.get("all_runs_passed", False)), r.get("error", ""), str(r.get("qa_latency_s", ""))])
        for run in r.get("runs", []):
            runs.append([r.get("id", ""), str(run.get("run_index", "")), run.get("code", ""),
                         run.get("stdout", ""), run.get("stderr", ""), str(run.get("exit_code", "")),
                         str(run.get("duration_ms", "")), run.get("failure_summary", "")])
    return [("Responses", responses), ("Sandbox Runs", runs)]


def _is_stale_session_error(e: Exception) -> bool:
    """True for the expired-session-token signature: an empty-body 500 from
    /api/chatkit. conversation-server-app's AuthTokenController 401s internally
    on an expired/invalid session authToken, which the outer ChatKitController
    turns into a bodyless 500 — indistinguishable from a real transient 5xx by
    status code alone, but permanent, not transient: retrying the same dead
    token never succeeds. Confirmed by cross-referencing the container log at
    the time of failure: 'Invalid or expired user access token' /
    'ResponseStatusException: 401 UNAUTHORIZED "Invalid or expired user token"'."""
    return (
        isinstance(e, httpx.HTTPStatusError)
        and e.response.status_code == 500
        and not e.response.text.strip()
    )


def _refresh_session_file(session_file: Path) -> None:
    """Re-capture session.json by re-running init_session.py against the live LBF client."""
    print(f"     stale session detected — re-running init_session.py to refresh {session_file.name}...")
    result = subprocess.run(
        ["python3", str(HERE / "init_session.py"), "--base-url", os.getenv("CS_BASE_URL", "http://localhost:8040")],
        capture_output=True, text=True,
    )
    print(result.stdout.strip())
    if result.returncode != 0:
        print(result.stderr.strip())
        raise RuntimeError(f"init_session.py failed (exit {result.returncode}) — cannot refresh {session_file.name}")


def _retry_transient(fn, attempts: int, backoff: float, label: str, client: "ChatKitClient | None" = None):
    """Call fn(), retrying on transient 5xx / connection errors (e.g. the
    threads.create handshake to the instance failing under burst load).

    A stale-session 500 (see _is_stale_session_error) is NOT transient — it is
    the same dead authToken every time, so blind retries always exhaust and
    waste ~1-2 minutes per case. When `client` is given and carries a
    session_file, one stale-session hit re-captures session.json via
    init_session.py, reloads it into `client`, and retries immediately —
    outside the normal backoff/attempts budget, since this path either fixes
    the problem on the first try or the environment (LBF client / login) is
    down and no amount of retrying will help.
    """
    last = None
    refreshed_once = False
    for a in range(1, attempts + 1):
        try:
            return fn()
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            last = e
            if client is not None and client.session_file and not refreshed_once and _is_stale_session_error(e):
                refreshed_once = True
                print(f"     {label}: stale-session error (not transient) — refreshing session, not retrying blindly")
                _refresh_session_file(client.session_file)
                client.reload_session_from_disk()
                continue
            transient = isinstance(e, httpx.RequestError) or (
                isinstance(e, httpx.HTTPStatusError) and 500 <= e.response.status_code < 600
            )
            if a < attempts and transient:
                wait = backoff * a
                print(f"     transient {label} error ({type(e).__name__}); retry {a}/{attempts - 1} in {wait:.1f}s")
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"{label}: retries exhausted") from last  # pragma: no cover


def _case_query(case: dict, field: str) -> str:
    """The prompt to send for this case, from the caller-chosen field.

    Cases can carry the same question in several languages (test_query,
    test_query_en, test_query_ja); --query-field picks which one runs.
    """
    return (case.get(field) or "").strip()


def _human_size(num_bytes: int) -> str:
    """Human-readable size, e.g. '59.0 MB'. 0/unknown → ''."""
    if not num_bytes:
        return ""
    val = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024 or unit == "GB":
            return f"{val:.1f} {unit}"
        val /= 1024


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", default="office", help="office | pdf | combo | multiple | all")
    ap.add_argument("--limit", type=int, default=0,
                    help="max cases to run (0 = all matching cases)")
    ap.add_argument("--case-id", default="", help="run specific case id(s), comma-separated (overrides --limit)")
    ap.add_argument("--start-index", type=int, default=0,
                    help="1-based index into the selected/ordered case list to start from "
                         "(matches the [N/total] progress counter), e.g. --start-index 65 "
                         "resumes from the 65th case onward. Applied after --category/--file "
                         "filtering, before --limit.")
    ap.add_argument("--file", default="", help="filter cases by file name substring (e.g. 'capacity_planning')")
    ap.add_argument("--query-field", default="test_query",
                    help="case field holding the prompt to send (default: test_query). Use e.g. "
                         "test_query_en or test_query_ja to run the same cases in another language.")
    ap.add_argument("--cases", default=str(CASES_JSON),
                    help="path to the cases .json to run (default: scripts/cases.json). Relative "
                         "file_path/files entries inside it still resolve against the dataset root, "
                         "so an out-of-tree cases file should use absolute paths.")
    ap.add_argument("--base-url", default=os.getenv("CS_BASE_URL", "http://localhost:8040"))
    ap.add_argument("--session-file", default=str(HERE / "session.json"),
                    help="captured UI session metadata block (sessionId/userId/etc.)")
    ap.add_argument("--reuse-conversation", action="store_true",
                    help="reuse session.json's conversationId instead of threads.create per case")
    ap.add_argument("--check-session", action="store_true",
                    help="ping the session (threads.list) and report alive/expired, then exit")
    ap.add_argument("--timeout", type=float, default=1200.0)
    ap.add_argument("--delay", type=float, default=0.5,
                    help="seconds to wait between cases (avoid hammering the instance handshake)")
    ap.add_argument("--retries", type=int, default=5,
                    help="attempts for transient 5xx/429/connection/timeout errors on thread create (handshake)")
    ap.add_argument("--no-skills", action="store_true",
                    help="disable file-reading skills (read-csv/docx/pdf/pptx/xlsx) by moving them out during run")
    ap.add_argument("--runs", type=int, default=1,
                    help="number of times to run the full eval (each saved separately to capture variations)")
    ap.add_argument("--force", action="store_true",
                    help="ignore resume cache — re-run all cases even if JSONL sidecar has them")
    ap.add_argument("--instance", default="local",
                    help="instance label for output file naming (e.g. 'local', 'cidtest1nextmaria2')")
    ap.add_argument("--parallel", type=int, default=1,
                    help="number of cases to run concurrently (default 1 = sequential)")
    ap.add_argument("--rerun-zero", default="",
                    help="path to existing .jsonl — re-run cases with 0 sandbox runs or empty final answer, update in-place")
    ap.add_argument("--out", default=str(HERE / "eval_responses.xlsx"))
    args = ap.parse_args()

    cases_path = Path(args.cases)
    if not cases_path.exists():
        raise SystemExit(f"cases file not found: {cases_path}")

    session_file_path = Path(args.session_file)
    session = json.loads(session_file_path.read_text())

    if args.check_session:
        alive, detail = ChatKitClient(args.base_url, session, 20.0, session_file=session_file_path).check_session()
        print(("SESSION ALIVE  " if alive else "SESSION EXPIRED  ") + detail)
        raise SystemExit(0 if alive else 1)

    # --rerun-zero: read existing JSONL(s) from a directory, find cases with 0 sandbox runs
    # or empty final answer, re-run them, update the same JSONL + XLSX in-place.
    if args.rerun_zero:
        rerun_path = Path(args.rerun_zero)
        if not rerun_path.exists():
            raise SystemExit(f"Path not found: {rerun_path}")
        jsonl_files = sorted(rerun_path.glob("*.jsonl")) if rerun_path.is_dir() else [rerun_path]
        if not jsonl_files:
            raise SystemExit(f"No .jsonl files found in {rerun_path}")
        all_cases = json.loads(cases_path.read_text())
        cases_by_id = {c["id"]: c for c in all_cases}
        DEBUG_DIR.mkdir(exist_ok=True)
        client = ChatKitClient(args.base_url, session, args.timeout, session_file=session_file_path)
        for jsonl_file in jsonl_files:
            existing_records = _load_records(jsonl_file)
            deduped = {r["id"]: r for r in existing_records}
            zero_ids = {rid for rid, r in deduped.items()
                        if r.get("num_runs", 0) == 0 or not r.get("final_answer") or r.get("error")}
            # Always dedup and rewrite the file
            if len(existing_records) != len(deduped):
                with jsonl_file.open("w", encoding="utf-8") as f:
                    for rec in deduped.values():
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                print(f"\n{jsonl_file.name}: deduped {len(existing_records)} -> {len(deduped)} records.")
            if not zero_ids:
                print(f"\n{jsonl_file.name}: all cases OK — skipping.")
                continue
            rerun_cases = [cases_by_id[cid] for cid in zero_ids if cid in cases_by_id]
            if not rerun_cases:
                print(f"\n{jsonl_file.name}: {len(zero_ids)} zero-run IDs not found in cases.json — skipping.")
                continue
            print(f"\n{'='*60}")
            print(f"{jsonl_file.name}: re-running {len(rerun_cases)} case(s)")
            print(f"{'='*60}")
            records_by_id_rerun: dict[str, dict] = {r["id"]: r for r in existing_records}
            rerun_t0 = time.time()
            rerun_lock = threading.Lock()
            agent_run_dir = AGENT_OUTPUT_ROOT / f"rerun_{time.strftime('%Y%m%d_%H%M%S')}"

            def _rerun_case(idx: int, c: dict) -> dict:
                cid = c["id"]
                file_paths = [(PROJECT_ROOT / p) if not Path(p).is_absolute() else Path(p) for p in (c.get("files") or [c["file_path"]])]
                file_size = _human_size(sum(p.stat().st_size for p in file_paths if p.exists()))
                print(f"[{idx}/{len(rerun_cases)}] {cid}  {c['file_name']}")
                err = ""
                final_answer = ""
                runs: list[dict] = []
                provider_info = {"provider": "", "provider_type": ""}
                skills_used: list[str] = []
                qa_latency_s = None
                t0 = time.time()
                try:
                    missing = [str(p) for p in file_paths if not p.exists()]
                    if missing:
                        raise FileNotFoundError(f"file(s) not found: {missing}")
                    conversation_id = session["conversationId"] if args.reuse_conversation else _retry_transient(
                        client.create_thread, args.retries, 2.0, "create_thread", client=client)
                    print(f"  [{cid}] conversation: {conversation_id}")
                    attachments = [client.upload(conversation_id, p) for p in file_paths]
                    print(f"  [{cid}] uploaded {len(attachments)} attachment(s) ({file_size}): {[a.get('id') for a in attachments]}")
                    qa_t0 = time.time()
                    final_answer = client.send_message_sse(
                        conversation_id, attachments, _case_query(c, args.query_field), DEBUG_DIR / f"{cid}_rerun.sse"
                    )
                    qa_latency_s = round(time.time() - qa_t0, 1)
                    runs = read_sandbox_runs(conversation_id, t0)
                    provider_info = read_provider(conversation_id, t0)
                    skills_used = read_skills(conversation_id, t0)
                    write_trace(conversation_id, t0, DEBUG_DIR / f"{cid}_rerun.trace")
                    trunc_runs = [r for r in runs if r.get("truncated")]
                    total_out = sum(r.get("output_bytes", 0) for r in runs)
                    out_tag = f" | output: {round(total_out/1024,1)}KB" if total_out else ""
                    if trunc_runs:
                        sizes = ", ".join(f"{round(r.get('output_bytes',0)/1024,1)}KB" for r in trunc_runs)
                        out_tag += f" | TRUNCATED: {len(trunc_runs)}/{len(runs)} (original: {sizes})"
                    skills_tag = f" | skills: {', '.join(skills_used)}" if skills_used else " | skills: none"
                    print(f"  [{cid}] final answer: {len(final_answer)} chars | sandbox runs: {len(runs)} | provider: {provider_info['provider']}{skills_tag}{out_tag} | qa_latency: {qa_latency_s}s")
                except Exception as e:  # noqa: BLE001
                    err = f"{type(e).__name__}: {e}"
                    print(f"  [{cid}] ERROR: {err}")
                elapsed_s = round(time.time() - t0, 1)
                print(f"  [{cid}] elapsed: {elapsed_s}s")
                all_pass = bool(runs) and all(str(r["exit_code"]) == "0" for r in runs)
                record = {
                    "id": cid, "category": c["category"], "modality": c.get("modality", "sandbox"),
                    "file": c["file_name"], "file_size": file_size,
                    "query_type": c["query_type"], "test_query": _case_query(c, args.query_field),
                    "query_field": args.query_field,
                    "gold_standard": c["gold_standard"], "final_answer": final_answer,
                    "provider": provider_info["provider"], "provider_type": provider_info["provider_type"],
                    "skills_used": skills_used,
                    "num_runs": len(runs), "all_runs_passed": all_pass, "error": err,
                    "elapsed_s": elapsed_s, "qa_latency_s": qa_latency_s,
                    "runs": [
                        {"run_index": ri, "code": r["code"], "stdout": r["stdout"], "stderr": r["stderr"],
                         "exit_code": r["exit_code"], "duration_ms": r["duration_ms"],
                         "truncated": r.get("truncated", False), "output_bytes": r.get("output_bytes", 0),
                         "output_pct": r.get("output_pct", ""),
                         "failure_summary": failure_summary(r["stderr"], r["exit_code"])}
                        for ri, r in enumerate(runs, 1)
                    ],
                }
                with rerun_lock:
                    records_by_id_rerun[cid] = record
                write_invoice_agent_output(cid, final_answer, agent_run_dir)
                write_classification_output(cid, final_answer, agent_run_dir)
                return record

            if args.parallel > 1:
                print(f"Running {len(rerun_cases)} rerun cases with {args.parallel} workers...")
                with ThreadPoolExecutor(max_workers=args.parallel) as pool:
                    futures = {pool.submit(_rerun_case, i, c): c for i, c in enumerate(rerun_cases, 1)}
                    for fut in as_completed(futures):
                        fut.result()
            else:
                for i, c in enumerate(rerun_cases, 1):
                    _rerun_case(i, c)
                    if args.delay and i < len(rerun_cases):
                        time.sleep(args.delay)
            # Rewrite JSONL and XLSX with updated records
            with jsonl_file.open("w", encoding="utf-8") as f:
                for rec in records_by_id_rerun.values():
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            # XLSX name: strip the .jsonl suffix (file is something.xlsx.jsonl)
            xlsx_path = jsonl_file.with_suffix("") if str(jsonl_file).endswith(".xlsx.jsonl") else jsonl_file.with_suffix(".xlsx")
            write_xlsx(xlsx_path, _records_to_sheets(list(records_by_id_rerun.values())))
            rerun_elapsed = round(time.time() - rerun_t0, 1)
            still_zero = sum(1 for cid in zero_ids if records_by_id_rerun.get(cid, {}).get("num_runs", 0) == 0)
            print(f"  {len(rerun_cases)} re-run, {still_zero} still zero | total time: {rerun_elapsed}s -> {jsonl_file.name}")
        raise SystemExit(0)

    all_cases = json.loads(cases_path.read_text())
    cat_order = [x.strip() for x in args.category.split(",") if x.strip()]
    selected_cats = set(cat_order)
    cases = all_cases if "all" in selected_cats else [c for c in all_cases if c["category"] in selected_cats]
    # Preserve the order categories were passed on the CLI (stable within each category).
    if "all" not in selected_cats:
        cat_rank = {cat: rank for rank, cat in enumerate(cat_order)}
        cases.sort(key=lambda c: cat_rank.get(c["category"], len(cat_rank)))
    if args.case_id:
        wanted = {x.strip() for x in args.case_id.split(",")}
        cases = [c for c in cases if c["id"] in wanted]
    if args.file:
        cases = [c for c in cases if args.file.lower() in c.get("file_name", "").lower()]
    start_index_excluded_ids: set[str] = set()
    if args.start_index > 0 and not args.case_id:
        if args.start_index > len(cases):
            raise SystemExit(f"--start-index {args.start_index} exceeds {len(cases)} matching case(s).")
        print(f"--start-index {args.start_index}: skipping first {args.start_index - 1} case(s).")
        start_index_excluded_ids = {c["id"] for c in cases[: args.start_index - 1]}
        cases = cases[args.start_index - 1:]
    if args.limit > 0 and not args.case_id:
        cases = cases[: args.limit]
    if not cases:
        raise SystemExit(f"No cases for category={args.category}. Available: {sorted(set(c['category'] for c in all_cases))}")

    # Fail before burning a single turn if the chosen query field is absent/blank —
    # otherwise every case sends an empty prompt and the whole run is wasted.
    blank = [c["id"] for c in cases if not _case_query(c, args.query_field)]
    if blank:
        fields = sorted({k for c in cases for k in c if k.startswith("test_query")})
        raise SystemExit(
            f"--query-field '{args.query_field}' is missing or empty on {len(blank)} of {len(cases)} "
            f"case(s): {blank[:5]}{'...' if len(blank) > 5 else ''}\n"
            f"Query fields present in this cases file: {fields}"
        )
    print(f"Query field: {args.query_field}")

    # Determine run label
    if args.case_id:
        run_label = "cases_" + "_".join(sorted(x.strip() for x in args.case_id.split(",")))[:60]
    else:
        run_label = "_".join(sorted(selected_cats))

    print(f"Matched {len(cases)} case(s) for '{run_label}'.")

    DEBUG_DIR.mkdir(exist_ok=True)
    client = ChatKitClient(args.base_url, session, args.timeout, session_file=session_file_path)

    # Hide file-reading skills if --no-skills (prefix with _ so loader skips them)
    skills_renamed: list[tuple[Path, Path]] = []
    if args.no_skills:
        for skill_name in FILE_SKILLS:
            src = SKILLS_CORE_DIR / skill_name
            dst = SKILLS_CORE_DIR / f"_{skill_name}"
            if src.exists():
                src.rename(dst)
                skills_renamed.append((src, dst))
        if skills_renamed:
            print(f"Disabled {len(skills_renamed)} file skills: {[s.name for s, _ in skills_renamed]}")
            print("Restarting AO container to rebuild skill catalog...")
            subprocess.run(["docker", "restart", AO_V2_CONTAINER], check=True, capture_output=True)
            time.sleep(8)
            print("AO container restarted.")
        else:
            print("No file skills found to disable — skipping container restart.")
        skills_label = "noskills"
    else:
        skills_label = "withskills"

    try:
      for run_num in range(1, args.runs + 1):
        if args.runs > 1:
            print(f"\n{'='*60}")
            print(f"RUN {run_num}/{args.runs}")
            print(f"{'='*60}")

        run_suffix = f"_run{run_num}" if args.runs > 1 else ""
        ts = time.strftime("%Y%m%d_%H%M%S")
        if args.out == str(HERE / "eval_responses.xlsx"):
            run_dir = PROJECT_ROOT / "results" / run_label / skills_label / f"{ts}{run_suffix}"
            run_dir.mkdir(parents=True, exist_ok=True)
            out_path = run_dir / "responses.xlsx"
        else:
            base = Path(args.out)
            out_path = base.with_stem(f"{base.stem}{run_suffix}") if args.runs > 1 else base
        progress_path = out_path.with_suffix(out_path.suffix + ".jsonl")
        # Own timestamped folder for invoice agent_output — never overwrites a prior run.
        # On resume (progress JSONL already has records), reuse the SAME agent_run_dir
        # as the original run instead of minting a new timestamp, so already-done
        # cases' agent_output JSON and newly-processed cases' JSON end up in one
        # complete folder rather than split across two partial ones.
        agent_run_dir_marker = progress_path.with_suffix(progress_path.suffix + ".agent_run_dir")
        agent_run_dir = AGENT_OUTPUT_ROOT / f"{ts}{run_suffix}"

        records_by_id: dict[str, dict] = {}
        if args.force:
            print("--force: ignoring resume cache, all cases will re-run.")
            if progress_path.exists():
                progress_path.unlink()
                print(f"  Deleted stale {progress_path.name}")
            if agent_run_dir_marker.exists():
                agent_run_dir_marker.unlink()
        elif args.runs == 1:
            for r in _load_records(progress_path):
                records_by_id[r.get("id")] = r
            if args.start_index > 0 and records_by_id:
                # --start-index means "run this window, full stop": permanently drop
                # the skipped cases' (1..start_index-1) records from the JSONL/xlsx on
                # disk, AND drop any existing records for cases INSIDE the window too
                # (65+) so none of them are treated as already-done — every case from
                # start_index onward reruns and overwrites its record in place (the
                # per-case write path below already replaces-by-id, so this produces
                # a substitution, not a duplicate).
                in_window_ids = {c["id"] for c in cases}
                drop_ids = (start_index_excluded_ids | in_window_ids) & set(records_by_id)
                if drop_ids:
                    print(f"--start-index: permanently removing {len(drop_ids)} record(s) "
                          f"({len(start_index_excluded_ids & set(records_by_id))} skipped-case, "
                          f"{len(in_window_ids & set(records_by_id))} in-window) from "
                          f"{progress_path.name} and its xlsx — the whole 65+ window reruns.")
                    for cid in drop_ids:
                        del records_by_id[cid]
                    with progress_path.open("w", encoding="utf-8") as f:
                        for r in records_by_id.values():
                            f.write(json.dumps(r, ensure_ascii=False) + "\n")
                    write_xlsx(out_path, _records_to_sheets(list(records_by_id.values())))
            if agent_run_dir_marker.exists():
                prior_dir = Path(agent_run_dir_marker.read_text().strip())
                if prior_dir.exists():
                    agent_run_dir = prior_dir
                    print(f"Resuming agent_output into prior run folder: {agent_run_dir}")

        agent_run_dir.mkdir(parents=True, exist_ok=True)
        agent_run_dir_marker.write_text(str(agent_run_dir))

        def _is_done(r: dict) -> bool:
            return not r.get("error") and bool(r.get("final_answer") or r.get("num_runs", 0))
        # Scope resume accounting to the current case window (--case-id/--file/
        # --start-index/--limit) — a JSONL from a prior run may hold done records
        # for cases outside this window; those must not appear in done/retry counts
        # or affect what's reported as "in scope" here.
        in_scope_ids = {c["id"] for c in cases}
        done_ids = {i for i, r in records_by_id.items() if i in in_scope_ids and _is_done(r)}
        retry_ids = {i for i in records_by_id if i in in_scope_ids} - done_ids
        if records_by_id:
            print(f"Resuming from {progress_path.name}: {len(done_ids)} done, "
                  f"{len(retry_ids)} previously-errored will be retried.")
        pending = [c for c in cases if c["id"] not in done_ids]
        print(f"{len(pending)} case(s) to run ({len(cases)} in scope).")
        run_t0 = time.time()
        write_lock = threading.Lock()

        # Pre-created conversationIds: case_id -> conversationId
        pre_created_convs: dict[str, str] = {}

        def _pre_create_batch(batch: list[dict], offset: int) -> None:
            """Pre-create threads for a batch of cases sequentially."""
            for j, c in enumerate(batch, 1):
                cid = c["id"]
                try:
                    conv_id = _retry_transient(client.create_thread, args.retries, 2.0, "create_thread", client=client)
                    client.reset_reusable(conv_id)
                    pre_created_convs[cid] = conv_id
                    print(f"  [{offset + j}/{len(pending)}] {cid} -> {conv_id}")
                except Exception as e:  # noqa: BLE001
                    print(f"  [{offset + j}/{len(pending)}] {cid} -> FAILED: {e}")

        def _run_case(idx: int, c: dict) -> dict:
            """Run a single eval case. Thread-safe — writes are guarded by write_lock."""
            cid = c["id"]
            file_paths = [(PROJECT_ROOT / p) if not Path(p).is_absolute() else Path(p) for p in (c.get("files") or [c["file_path"]])]
            file_size = _human_size(sum(p.stat().st_size for p in file_paths if p.exists()))
            print(f"[{idx}/{len(pending)}] {cid}  {c['file_name']}")
            err = ""
            final_answer = ""
            runs: list[dict] = []
            provider_info = {"provider": "", "provider_type": ""}
            skills_used: list[str] = []
            qa_latency_s = None
            t0 = time.time()
            try:
                missing = [str(p) for p in file_paths if not p.exists()]
                if missing:
                    raise FileNotFoundError(f"file(s) not found: {missing}")
                if args.reuse_conversation:
                    conversation_id = session["conversationId"]
                elif cid in pre_created_convs:
                    conversation_id = pre_created_convs[cid]
                else:
                    conversation_id = _retry_transient(
                        client.create_thread, args.retries, 2.0, "create_thread", client=client)
                print(f"  [{cid}] conversation: {conversation_id}")
                attachments = [client.upload(conversation_id, p) for p in file_paths]
                print(f"  [{cid}] uploaded {len(attachments)} attachment(s) ({file_size}): {[a.get('id') for a in attachments]}")
                qa_t0 = time.time()
                final_answer = client.send_message_sse(
                    conversation_id, attachments, _case_query(c, args.query_field), DEBUG_DIR / f"{cid}{run_suffix}.sse"
                )
                qa_latency_s = round(time.time() - qa_t0, 1)
                runs = read_sandbox_runs(conversation_id, t0)
                provider_info = read_provider(conversation_id, t0)
                skills_used = read_skills(conversation_id, t0)
                write_trace(conversation_id, t0, DEBUG_DIR / f"{cid}{run_suffix}.trace")
                trunc_runs = [r for r in runs if r.get("truncated")]
                total_out = sum(r.get("output_bytes", 0) for r in runs)
                out_tag = f" | output: {round(total_out/1024,1)}KB" if total_out else ""
                if trunc_runs:
                    sizes = ", ".join(f"{round(r.get('output_bytes',0)/1024,1)}KB" for r in trunc_runs)
                    out_tag += f" | TRUNCATED: {len(trunc_runs)}/{len(runs)} (original: {sizes})"
                skills_tag = f" | skills: {', '.join(skills_used)}" if skills_used else " | skills: none"
                print(f"  [{cid}] final answer: {len(final_answer)} chars | sandbox runs: {len(runs)} | provider: {provider_info['provider']}{skills_tag}{out_tag} | qa_latency: {qa_latency_s}s")
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
                print(f"  [{cid}] ERROR: {err}")

            elapsed_s = round(time.time() - t0, 1)
            print(f"  [{cid}] elapsed: {elapsed_s}s")
            all_pass = bool(runs) and all(str(r["exit_code"]) == "0" for r in runs)
            record = {
                "id": cid, "category": c["category"], "modality": c.get("modality", "sandbox"),
                "file": c["file_name"], "file_size": file_size,
                "query_type": c["query_type"], "test_query": _case_query(c, args.query_field),
                "query_field": args.query_field,
                "gold_standard": c["gold_standard"], "final_answer": final_answer,
                "provider": provider_info["provider"], "provider_type": provider_info["provider_type"],
                "skills_used": skills_used,
                "num_runs": len(runs), "all_runs_passed": all_pass, "error": err,
                "elapsed_s": elapsed_s, "qa_latency_s": qa_latency_s, "run_number": run_num,
                "runs": [
                    {"run_index": ri, "code": r["code"], "stdout": r["stdout"], "stderr": r["stderr"],
                     "exit_code": r["exit_code"], "duration_ms": r["duration_ms"],
                     "truncated": r.get("truncated", False), "output_bytes": r.get("output_bytes", 0),
                     "output_pct": r.get("output_pct", ""),
                     "failure_summary": failure_summary(r["stderr"], r["exit_code"])}
                    for ri, r in enumerate(runs, 1)
                ],
            }
            with write_lock:
                with progress_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                records_by_id[cid] = record
                write_xlsx(out_path, _records_to_sheets(list(records_by_id.values())))
            write_invoice_agent_output(cid, final_answer, agent_run_dir)
            write_classification_output(cid, final_answer, agent_run_dir)
            return record

        def _retry_zero_cases(cat: str, cat_cases: list[dict]) -> None:
            """Retry cases that got 0 sandbox runs or 0-char final_answer.
            Image cases go through vision, not sandbox — zero sandbox runs is
            expected, so only retry on zero-char final_answer for that category."""
            if cat == "image":
                zero_ids = [
                    cid for cid, r in records_by_id.items()
                    if r["category"] == cat and not r.get("final_answer")
                ]
            else:
                zero_ids = [
                    cid for cid, r in records_by_id.items()
                    if r["category"] == cat and (r.get("num_runs", 0) == 0 or not r.get("final_answer"))
                ]
            if not zero_ids:
                return
            retry_cases = [c for c in cat_cases if c["id"] in zero_ids]
            print(f"\n  Retrying {len(retry_cases)} zero-run/zero-answer cases for '{cat}'...")
            # Pre-create fresh threads for retries
            for c in retry_cases:
                cid = c["id"]
                try:
                    conv_id = _retry_transient(client.create_thread, args.retries, 2.0, "create_thread", client=client)
                    client.reset_reusable(conv_id)
                    pre_created_convs[cid] = conv_id
                except Exception:  # noqa: BLE001
                    pass
            if args.parallel > 1:
                with ThreadPoolExecutor(max_workers=args.parallel) as pool:
                    futures = {pool.submit(_run_case, 0, c): c for c in retry_cases}
                    for fut in as_completed(futures):
                        fut.result()
            else:
                for c in retry_cases:
                    _run_case(0, c)
            still_zero = sum(1 for cid in zero_ids
                             if records_by_id.get(cid, {}).get("num_runs", 0) == 0
                             or not records_by_id.get(cid, {}).get("final_answer"))
            print(f"  Retry done: {len(zero_ids) - still_zero}/{len(zero_ids)} recovered, {still_zero} still zero.")

        def _judge_category(cat: str, base_out: Path, all_records: dict, rn: int) -> None:
            """Write per-category JSONL + XLSX and auto-judge."""
            recs = [r for r in all_records.values() if r["category"] == cat]
            if not recs:
                return
            cat_dir = base_out.parent / cat
            cat_dir.mkdir(parents=True, exist_ok=True)
            cat_jsonl = cat_dir / "responses.xlsx.jsonl"
            with cat_jsonl.open("w", encoding="utf-8") as f:
                for r in recs:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            write_xlsx(cat_dir / "responses.xlsx", _records_to_sheets(recs))
            errored = sum(1 for r in recs if r.get("error"))
            print(f"\n  {cat}: {len(recs)} cases ({errored} errored) -> {cat_dir}")
            try:
                from run_judge_batch import _judge_jsonl
                run_lbl = f"run{rn}"
                print(f"  --- Judge ({cat}, {run_lbl}) ---")
                _judge_jsonl(cat_jsonl, cat, run_lbl)
            except Exception as e:  # noqa: BLE001
                print(f"  Judge auto-run failed for {cat}: {e}")

        if args.parallel > 1:
            # Group pending cases by category, pre-create + run per category
            from collections import OrderedDict
            cat_groups: OrderedDict[str, list[tuple[int, dict]]] = OrderedDict()
            for i, c in enumerate(pending, 1):
                cat_groups.setdefault(c["category"], []).append((i, c))

            print(f"Running {len(pending)} cases with {args.parallel} parallel workers "
                  f"across {len(cat_groups)} category batch(es)...")
            for cat, indexed_cases in cat_groups.items():
                batch_cases = [c for _, c in indexed_cases]
                offset = indexed_cases[0][0] - 1
                print(f"\n--- Category '{cat}': {len(batch_cases)} cases ---")
                if not args.reuse_conversation:
                    _pre_create_batch(batch_cases, offset)
                with ThreadPoolExecutor(max_workers=args.parallel) as pool:
                    futures = {pool.submit(_run_case, i, c): c for i, c in indexed_cases}
                    for fut in as_completed(futures):
                        fut.result()
                # Retry zero-run/zero-answer cases, then judge
                _retry_zero_cases(cat, batch_cases)
                _judge_category(cat, out_path, records_by_id, run_num)
        else:
            for i, c in enumerate(pending, 1):
                _run_case(i, c)
                if args.delay and i < len(pending):
                    time.sleep(args.delay)

        # For sequential mode, retry zeros + judge per category at end
        if args.parallel <= 1:
            seen_cats = sorted({r["category"] for r in records_by_id.values()})
            for cat in seen_cats:
                cat_cases = [c for c in pending if c["category"] == cat]
                _retry_zero_cases(cat, cat_cases)
                _judge_category(cat, out_path, records_by_id, run_num)

        run_elapsed = round(time.time() - run_t0, 1)
        errored = sum(1 for r in records_by_id.values() if r.get("error"))
        print(f"\nRun {run_num} done. {len(records_by_id)} cases ({errored} errored) | total time: {run_elapsed}s -> {out_path}")
        print(f"Durable progress -> {progress_path}")
        print(f"Raw SSE dumps -> {DEBUG_DIR}/")
    finally:
        for src, dst in skills_renamed:
            if dst.exists():
                dst.rename(src)
        if skills_renamed:
            print(f"Restored {len(skills_renamed)} file skills.")
            print("Restarting AO container to restore skill catalog...")
            subprocess.run(["docker", "restart", AO_V2_CONTAINER], check=True, capture_output=True)
            time.sleep(8)
            print("AO container restarted with skills restored.")


if __name__ == "__main__":
    main()
