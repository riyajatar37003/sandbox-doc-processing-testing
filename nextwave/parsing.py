"""Pure helpers for digging values out of SSE event JSON and assembling text."""

from __future__ import annotations

from typing import Any


def dig(obj: dict[str, Any], *paths: Any) -> str:
    """Return the first present value among flat keys or (parent, child) tuples."""
    for p in paths:
        if isinstance(p, tuple):
            cur: Any = obj
            for key in p:
                if not isinstance(cur, dict):
                    cur = None
                    break
                cur = cur.get(key)
            if cur:
                return str(cur)
        else:
            v = obj.get(p)
            if v:
                return str(v)
    return ""


def event_type_histogram(events: list[dict[str, Any]]) -> dict[str, int]:
    """Count SSE events by their `type` (or `object`) — a compact trace summary."""
    hist: dict[str, int] = {}
    for ev in events:
        etype = str(ev.get("type") or ev.get("object") or "unknown")
        hist[etype] = hist.get(etype, 0) + 1
    return hist


def extract_assistant_text(events: list[dict[str, Any]]) -> str:
    """Reconstruct the assistant's reply from ChatKit SSE events.

    Concatenates `*.text_delta` deltas; falls back to the text content of a
    terminal `thread.item.done` / message item when no deltas were streamed.
    """
    deltas: list[str] = []
    for ev in events:
        etype = str(ev.get("type") or ev.get("object") or "")
        if etype.endswith("text_delta"):
            delta = ev.get("delta")
            if isinstance(delta, str):
                deltas.append(delta)
            elif isinstance(delta, dict) and isinstance(delta.get("text"), str):
                deltas.append(delta["text"])
    if deltas:
        return "".join(deltas).strip()

    # Fallback: scan terminal item events for assembled text content.
    # Skip user_message items -- those are just the echoed prompt we sent,
    # not a real assistant reply (seen when the stream degenerates to a
    # single echo event with no actual assistant turn).
    for ev in reversed(events):
        item = ev.get("item") if isinstance(ev.get("item"), dict) else ev
        if isinstance(item, dict) and item.get("type") == "user_message":
            continue
        text = _text_from_item(ev)
        if text:
            return text.strip()
    return ""


def stream_ended_abnormally(events: list[dict[str, Any]]) -> bool:
    """True when the SSE stream stopped mid-turn rather than finishing cleanly
    -- i.e. no real answer ever arrived, for reasons that have nothing to do
    with parsing.

    Root-caused against real captures (Gemini-backed model, `_gemini` output
    dir) that failed on every one of 3 turns in a live test, in two DIFFERENT
    shapes: one dump ended after 63 events, its last item a `workflow` step
    whose own `summary.streaming` flag was still True; another ended after
    just ONE event -- the echoed `user_message` itself, before any workflow
    step even started. `post_sse`'s reader loop has three intentional stop
    conditions (deadline reached, a `data: [DONE]` sentinel line, `max_events`
    reached) -- none fired in either capture (spans of seconds vs a 1200s
    deadline, max_events=2000 vs 1-63 actual events, no [DONE] seen). That
    only leaves one explanation both times: `resp.iter_lines()` stopped
    yielding lines because the underlying HTTP connection was closed early (by
    the server or something between client and server) before the turn's real
    answer was ever sent -- at very different points in the exchange, so
    checking the shape of the LAST event (as the first version of this
    function did) is too narrow. The one thing both captures share, and the
    one thing every clean, complete turn has (per the original single-echo
    comment in extract_assistant_text's fallback, which already named this
    same degenerate case): a real answer always arrives as an
    `assistant_message` item. Its total absence -- no matter how many events,
    or which kind, came before that -- is the general signal.
    """
    if not events:
        return False
    return not any(
        isinstance(ev.get("item"), dict) and ev["item"].get("type") == "assistant_message"
        for ev in events
    )


def extract_file_attachments(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pull generated-file attachments out of ChatKit SSE events.

    Confirmed from a live capture (raw_events dump): the ChatKit relay
    delivers a generated file (e.g. a PPT the agent created) inside a
    `thread.item.added` / `thread.item.done` event whose
    `item.type == "assistant_message"`, under `item.attachments`:

        {"id": <sys_attachment sys_id>, "name": ..., "size": ...,
         "mime_type": ..., "type": "file",
         "upload_url": "https://<host>/sys_attachment.do?sys_id=<id>",
         "onClickAction": {...}}

    No base64 involved — `upload_url` is a directly fetchable download
    link (standard ServiceNow attachment URL), gated by the same
    session/auth cookies used for the rest of the flow.
    """
    found: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for ev in events:
        if not isinstance(ev, dict):
            continue
        item = ev.get("item")
        if not isinstance(item, dict):
            continue
        attachments = item.get("attachments")
        if not isinstance(attachments, list):
            continue
        for att in attachments:
            if not isinstance(att, dict):
                continue
            att_id = att.get("id") or ""
            if att_id in seen_ids:
                continue
            seen_ids.add(att_id)
            found.append({
                "sys_id": att_id,
                "title": att.get("name") or "attachment",
                "mime_type": att.get("mime_type") or "application/octet-stream",
                "size": att.get("size") or 0,
                "download_url": att.get("upload_url") or "",
            })

    return found


def _text_from_item(ev: dict[str, Any]) -> str:
    item = ev.get("item") if isinstance(ev.get("item"), dict) else ev
    content = item.get("content") if isinstance(item, dict) else None
    if isinstance(content, list):
        parts = [
            c.get("text", "")
            for c in content
            if isinstance(c, dict) and isinstance(c.get("text"), str)
        ]
        if any(parts):
            return "".join(parts)
    if isinstance(content, str):
        return content
    if isinstance(item, dict) and isinstance(item.get("text"), str):
        return item["text"]
    return ""
