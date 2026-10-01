"""
AIA execution-trace fetcher.

After an utterance is answered, this pulls the orchestrator's own execution
trace from the instance via the Table API, so each eval record carries not just
the chat reply but *how* it was produced:

    sn_aia_execution_plan   where <plan_conversation_field> = conversation_id
    sn_aia_execution_task   where <task_plan_field>  IN (plan sys_ids)
    sn_aia_tools_execution  where <tool_plan_field>  IN (plan sys_ids)
    sn_aia_message          where <message_conversation_field> = conversation_id  (optional)

This is the prod-available analog of the dev-only `/debug` page: the execution
*tasks* are the persisted step/event trace, a computed `stats` block mirrors the
debug stats panel, and `sn_aia_message` (every message + tool output of the
execution) is the persisted transcript. (`/debug`'s VFS + skills panels read
cache/storage, not Glide tables, so they aren't reproducible per-conversation in
prod and are out of scope here.)

Column names default to what the agent-orchestrator-v2 domain models and the
table schema use, but every reference field is overridable (the live schema is
behind a cache layer, so defaults can't be verified from code alone).

Reuses the authenticated requests.Session from the transport (instance cookies
+ X-UserToken), and queries the *instance* base URL (not the chat host).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import NextWaveConfig
from .transport import SseTransport


@dataclass
class AiaTraceConfig:
    plan_table: str = "sn_aia_execution_plan"
    task_table: str = "sn_aia_execution_task"
    tool_table: str = "sn_aia_tools_execution"
    message_table: str = "sn_aia_message"
    # Reference columns used in the Table API queries. The plan's conversation
    # link is the `conversation` field (the `conversation_id` column exists but
    # is null on real rows); verified against the agent-orchestrator-v2 schema.
    plan_conversation_field: str = "conversation"
    task_plan_field: str = "execution_plan"
    tool_plan_field: str = "execution_plan"
    # The transcript table's conversation reference column is not documented in
    # code — confirm on your instance and override if needed. Off by default.
    fetch_messages: bool = False
    message_conversation_field: str = "conversation_id"
    # Generative-AI call log for the conversation (one row per model call),
    # keyed by `conversation`.
    fetch_genai_logs: bool = True
    genai_log_table: str = "sys_generative_ai_log"
    genai_log_conversation_field: str = "conversation"
    # GenAI logs lag the reply (~40s observed), so they get a longer poll budget.
    genai_log_poll_attempts: int = 12
    genai_log_poll_interval: float = 5.0
    # Every result set is ordered by this column ascending (oldest first).
    order_field: str = "sys_updated_on"
    # sysparm_display_value: "true" | "false" | "all". "all" returns both the
    # raw value and the display value for every field (richest for eval).
    display_value: str = "all"
    limit: int = 1000
    # Plans/tasks are written during the turn; allow a brief poll for them to land.
    poll_attempts: int = 5
    poll_interval: float = 2.0
    # Many columns (e.g. plan/task `metadata`, task `output`) hold JSON encoded as a
    # string — often double-escaped (an output JSON whose tool `content` is itself a
    # JSON string). Best-effort parse these into real JSON so the artifact is readable
    # and consumers don't have to repeatedly json.loads. Non-JSON values are untouched.
    parse_json_fields: bool = True
    # Caps only the string→parse unwrap chain (a JSON string that parses into another
    # JSON string, …), NOT ordinary dict/list traversal — so deeply-nested JSON strings
    # are still parsed. Real data nests ~2-3 unwraps deep; 20 is ample headroom.
    parse_json_max_unwrap: int = 20
    # Multi-line strings (e.g. a tool `command` holding a bash/python script) are stored
    # in JSON with literal "\n" on a single line — unreadable. Split them into a list of
    # lines so the artifact renders code line-by-line. Applied after JSON parsing.
    split_multiline_strings: bool = True


@dataclass
class AiaTrace:
    conversation_id: str
    plan_ids: list[str] = field(default_factory=list)
    plans: list[dict[str, Any]] = field(default_factory=list)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    tool_executions: list[dict[str, Any]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    genai_logs: list[dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "plan_ids": self.plan_ids,
            "counts": {
                "plans": len(self.plans),
                "tasks": len(self.tasks),
                "tool_executions": len(self.tool_executions),
                "messages": len(self.messages),
                "genai_logs": len(self.genai_logs),
            },
            # /debug-style stats panel, computed from the persisted records.
            "stats": _compute_stats(self.plans, self.tasks, self.tool_executions),
            "plans": self.plans,
            "tasks": self.tasks,
            "tool_executions": self.tool_executions,
            "messages": self.messages,
            "genai_logs": self.genai_logs,
            "error": self.error,
        }


class AiaTraceFetcher:
    def __init__(
        self,
        cfg: NextWaveConfig,
        transport: SseTransport,
        trace_cfg: Optional[AiaTraceConfig] = None,
    ):
        self.cfg = cfg
        self.t = transport
        self.tc = trace_cfg or AiaTraceConfig()

    # ---- public ------------------------------------------------------------

    def fetch(self, conversation_id: str) -> AiaTrace:
        """Fetch plan + tasks + tool executions for one conversation."""
        trace = AiaTrace(conversation_id=conversation_id)
        if not conversation_id:
            trace.error = "empty conversation_id"
            return trace

        try:
            plans = self._poll_for_plans(conversation_id)
            trace.plans = plans
            trace.plan_ids = [pid for pid in (_sys_id(p) for p in plans) if pid]

            # GenAI call log is keyed by the conversation directly, so capture it
            # regardless of whether an execution plan was produced.
            if self.tc.fetch_genai_logs:
                # These rows are flushed asynchronously a while after the reply
                # (~40s observed), so poll with a longer budget than plans.
                trace.genai_logs = self._poll_table(
                    self.tc.genai_log_table,
                    self._order(f"{self.tc.genai_log_conversation_field}={conversation_id}"),
                    self.tc.genai_log_poll_attempts,
                    self.tc.genai_log_poll_interval,
                    "waiting for genai logs to land",
                    {"conversation_id": conversation_id},
                )

            if not trace.plan_ids:
                self.t.emit(
                    "warn",
                    "aia",
                    "no execution plan found for conversation",
                    {
                        "conversation_id": conversation_id,
                        "field": self.tc.plan_conversation_field,
                        "genai_logs": len(trace.genai_logs),
                    },
                )
                self._parse_trace(trace)
                return trace

            trace.tasks = self._query_in(
                self.tc.task_table, self.tc.task_plan_field, trace.plan_ids
            )
            trace.tool_executions = self._query_in(
                self.tc.tool_table, self.tc.tool_plan_field, trace.plan_ids
            )
            if self.tc.fetch_messages:
                trace.messages = self._table_get(
                    self.tc.message_table,
                    self._order(f"{self.tc.message_conversation_field}={conversation_id}"),
                )
            self.t.emit(
                "info",
                "aia",
                "execution trace fetched",
                {
                    "conversation_id": conversation_id,
                    "plans": len(trace.plans),
                    "tasks": len(trace.tasks),
                    "tool_executions": len(trace.tool_executions),
                    "messages": len(trace.messages),
                    "genai_logs": len(trace.genai_logs),
                },
            )
        except Exception as e:  # noqa: BLE001 — trace capture must not fail the eval
            trace.error = str(e)
            self.t.emit(
                "error",
                "aia",
                "execution trace fetch failed",
                {"conversation_id": conversation_id, "error": str(e)},
            )
        self._parse_trace(trace)
        return trace

    # ---- internals ---------------------------------------------------------

    def _parse_trace(self, trace: AiaTrace) -> None:
        """Best-effort parse of JSON-string fields in every fetched record (in place).

        Columns like plan/task ``metadata`` and task ``output`` hold JSON encoded as a
        string (sometimes nested/double-escaped). Parsing makes the artifact readable and
        spares consumers repeated json.loads; non-JSON values are left untouched. Then,
        optionally, multi-line strings (e.g. a tool ``command`` script) are split into a
        list of lines so code renders line-by-line instead of one "\n"-laden blob.
        """
        if not (self.tc.parse_json_fields or self.tc.split_multiline_strings):
            return
        for records in (
            trace.plans,
            trace.tasks,
            trace.tool_executions,
            trace.messages,
            trace.genai_logs,
        ):
            for i, rec in enumerate(records):
                if self.tc.parse_json_fields:
                    rec = _deep_parse_json(rec, self.tc.parse_json_max_unwrap)
                if self.tc.split_multiline_strings:
                    rec = _split_multiline(rec)
                records[i] = rec

    def _order(self, query: str) -> str:
        """Append an ascending sys_updated_on (oldest-first) sort to a query."""
        return f"{query}^ORDERBY{self.tc.order_field}"

    def _poll_for_plans(self, conversation_id: str) -> list[dict[str, Any]]:
        return self._poll_table(
            self.tc.plan_table,
            self._order(f"{self.tc.plan_conversation_field}={conversation_id}"),
            self.tc.poll_attempts,
            self.tc.poll_interval,
            "waiting for execution plan to land",
            {"conversation_id": conversation_id},
        )

    def _poll_table(
        self,
        table: str,
        query: str,
        attempts: int,
        interval: float,
        wait_msg: str,
        emit_data: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Query a table, retrying until it returns rows (records can be written
        asynchronously after the turn) or the attempts are exhausted."""
        attempts = max(1, attempts)
        rows: list[dict[str, Any]] = []
        for attempt in range(1, attempts + 1):
            rows = self._table_get(table, query)
            if rows:
                return rows
            if attempt < attempts:
                self.t.emit(
                    "info", "aia", wait_msg, {**emit_data, "attempt": attempt, "of": attempts}
                )
                time.sleep(interval)
        return rows

    def _query_in(self, table: str, field_name: str, plan_ids: list[str]) -> list[dict[str, Any]]:
        if not plan_ids:
            return []
        # Encoded query: <field>IN<comma-separated sys_ids>
        query = self._order(f"{field_name}IN{','.join(plan_ids)}")
        return self._table_get(table, query)

    def _table_get(self, table: str, query: str) -> list[dict[str, Any]]:
        url = f"{self.t.base_url}/api/now/table/{table}"
        params = {
            "sysparm_query": query,
            "sysparm_display_value": self.tc.display_value,
            "sysparm_limit": str(self.tc.limit),
            "sysparm_exclude_reference_link": "true",
        }
        headers = {"Accept": "application/json", "X-UserToken": self.t.state.g_ck}
        resp = self.t.session.get(url, params=params, headers=headers, timeout=self.t.timeouts)
        if resp.status_code != 200:
            self.t.emit(
                "warn",
                "aia",
                "table query non-200",
                {
                    "table": table,
                    "status": resp.status_code,
                    "query": query,
                    "body": resp.text[:200],
                },
            )
            return []
        try:
            return (resp.json() or {}).get("result", []) or []
        except ValueError:
            self.t.emit("warn", "aia", "table query returned non-JSON", {"table": table})
            return []


def _sys_id(record: dict[str, Any]) -> str:
    """Extract sys_id whether the row is flat (display_value=false) or wrapped
    ({"value","display_value"} per field, as with display_value=all/true)."""
    v = record.get("sys_id")
    if isinstance(v, dict):
        return str(v.get("value", ""))
    return str(v) if v else ""


def _field(record: dict[str, Any], key: str) -> str:
    """Read a field value whether flat or wrapped ({value, display_value})."""
    v = record.get(key)
    if isinstance(v, dict):
        # After JSON-field parsing a wrapped value may itself be an object; fall back
        # to a JSON string so stats/category reads stay string-typed.
        dv = v.get("display_value")
        vv = v.get("value")
        chosen = dv or vv or ""
        return chosen if isinstance(chosen, str) else json.dumps(chosen)
    return str(v) if v not in (None, "") else ""


def _deep_parse_json(value: Any, max_unwrap: int) -> Any:
    """Recursively turn JSON-encoded strings into objects (best-effort, in place-safe).

    A string is parsed only when it looks like a JSON object/array (``{``/``[`` after
    strip); on any decode failure (e.g. a truncated tool-output preview) it is returned
    unchanged. dicts/lists are walked in full — ordinary container nesting does NOT count
    against the budget, so deeply-nested JSON strings (e.g. a tool call's
    ``function.arguments`` inside a parsed ``output``) are still unwrapped. Only the
    string→parse *unwrap chain* is bounded by ``max_unwrap`` to cap the pathological case
    of a string that parses into yet another JSON string, repeatedly.
    """
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in ("{", "["):
            try:
                parsed = json.loads(stripped)
            except (ValueError, TypeError):
                return value
            if max_unwrap <= 0:
                return parsed
            return _deep_parse_json(parsed, max_unwrap - 1)
        return value
    if isinstance(value, dict):
        return {k: _deep_parse_json(v, max_unwrap) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_parse_json(v, max_unwrap) for v in value]
    return value


def _split_multiline(value: Any) -> Any:
    """Render multi-line strings as a list of lines (recursively) for readability.

    A tool ``command`` holding a bash/python script — or any other multi-line field —
    is stored in JSON as one line with literal ``\\n`` escapes. Splitting it into a list
    of lines makes the artifact show the code line-by-line. Single-line strings and
    non-strings are returned unchanged. Uses ``splitlines()`` so no trailing empty entry.
    """
    if isinstance(value, str):
        return value.splitlines() if "\n" in value else value
    if isinstance(value, dict):
        return {k: _split_multiline(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_split_multiline(v) for v in value]
    return value


def _compute_stats(
    plans: list[dict[str, Any]], tasks: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> dict[str, Any]:
    """Summarize the execution like /debug's stats panel — purely from the
    persisted records, so it works against prod instances."""
    from collections import Counter

    _ERR = {"error", "failed", "failure", "cancelled", "terminated"}
    by_type = Counter(_field(t, "type") or "unknown" for t in tasks)
    by_status = Counter(_field(t, "status") or "unknown" for t in tasks)
    plan_states = Counter(_field(p, "state") or "unknown" for p in plans)
    errored = sum(1 for t in tasks if _field(t, "status").lower() in _ERR)
    return {
        "plans": len(plans),
        "tasks": len(tasks),
        "tool_executions": len(tools),
        "tasks_by_type": dict(by_type),
        "tasks_by_status": dict(by_status),
        "plans_by_state": dict(plan_states),
        "errored_tasks": errored,
    }
