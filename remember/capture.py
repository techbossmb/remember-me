#!/usr/bin/env python3
"""remember.me capture adapters: turn a surface's native transcript into generic entries
(id, kind, role, ts, text), and append them to an append-only raw log with dedup by entry id.

Adapters: Claude Code session .jsonl transcripts; claude.ai data export (conversations.json).
Used by cli.py (`remember ingest`, `remember import`, `remember hook`).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time

# ---------------------------------------------------------------- adapters

SYS_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
HARNESS_TAGS = re.compile(
    r"<(command-name|command-message|command-args|local-command-stdout)>.*?</\1>", re.S
)


def _clean(text):
    return HARNESS_TAGS.sub("", SYS_REMINDER.sub("", text)).strip()


def _text_of(content):
    """Only human-readable text; drop tool_use / tool_result / thinking blocks."""
    if isinstance(content, str):
        return _clean(content)
    parts = [b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]
    return _clean("\n\n".join(p for p in parts if p))


def read_claude_code(path):
    """Adapter: Claude Code transcript (.jsonl) -> (id, kind, role, ts, text).

    kind: msg | summary | compaction. Subagent (sidechain) traffic and harness-injected text are
    not conversation and are dropped; compaction summaries are kept separately.
    """
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f):
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = d.get("type")
            uid = d.get("uuid") or f"noid:{n}:{hash(line)}"
            ts = (d["timestamp"][:19] + "Z") if d.get("timestamp") else ""
            if d.get("isSidechain"):
                continue
            if t == "system" and (d.get("compactMetadata") or d.get("subtype") == "compact_boundary"):
                m = d.get("compactMetadata") or {}
                note = f"({m.get('trigger', '?')} compaction: {m.get('preTokens')} -> {m.get('postTokens')} tokens"
                if m.get("cumulativeDroppedTokens"):
                    note += f"; {m['cumulativeDroppedTokens']} dropped across session"
                yield uid, "compaction", "compaction", ts, note + "; summary in .compactions.md)"
                continue
            if t not in ("user", "assistant"):
                continue
            if d.get("isCompactSummary"):
                yield uid, "summary", "summary", ts, _text_of(d["message"]["content"])
                continue
            if d.get("isMeta") or d.get("isVisibleInTranscriptOnly"):
                continue
            txt = _text_of((d.get("message") or {}).get("content"))
            if txt:
                yield uid, "msg", t, ts, txt


def read_claude_export(path, conv):
    """Adapter: claude.ai data export (conversations.json) -> entries for one conversation (uuid or
    exact name). Text blocks verbatim; pasted attachments verbatim under a header; each tool call kept
    as a one-line stub so the log still says what was done. Thinking and tool results are dropped."""
    with open(path, encoding="utf-8") as f:
        convs = json.load(f)
    c = next(x for x in convs if conv in (x["uuid"], x.get("name")))
    for m in c.get("chat_messages", []):
        role = "user" if m.get("sender") == "human" else "assistant"
        parts = []
        for b in m.get("content") or []:
            if b.get("type") == "text" and b.get("text"):
                parts.append(b["text"])
            elif b.get("type") == "tool_use":
                inp = json.dumps(b.get("input", {}), ensure_ascii=False)
                parts.append(f"[tool: {b.get('name')}] {inp[:200]}{'…' if len(inp) > 200 else ''}")
        if not parts and m.get("text"):
            parts.append(m["text"])
        for a in m.get("attachments") or []:
            parts.append(f"[attachment: {a.get('file_name', '?')}]\n{a.get('extracted_content', '')}")
        for fl in m.get("files") or []:
            parts.append(f"[file: {fl.get('file_name', '?')}]")
        txt = _clean("\n\n".join(parts)).replace("```\nThis block is not supported on your current device yet.\n```", "").strip()
        if txt:
            ts = (m.get("created_at") or "")[:19] + "Z"
            yield m["uuid"], "msg", role, ts, txt


ADAPTERS = {"claude_code": read_claude_code}

# ---------------------------------------------------------------- append-only raw log


def append_log(entries, out_dir, session):
    """Append unseen entries to out_dir/<session>.md. Dedup by entry id, not by count, because
    harness transcripts get truncated (we saw 1710 lines shrink to 101). Returns #new messages."""
    os.makedirs(out_dir, exist_ok=True)
    log_path = os.path.join(out_dir, f"{session}.md")
    state_path = os.path.join(out_dir, f"{session}.state.json")
    state = {"seen": [], "last_id": 0, "last_role": None}
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
    seen = set(state["seen"])

    chunks, summaries, new = [], [], 0
    for uid, kind, role, ts, txt in entries:
        if uid in seen:
            continue
        seen.add(uid)
        state["seen"].append(uid)
        if kind == "summary":  # harness-written summary of earlier turns: keep it in the log, labelled
            summaries.append((ts, txt))
            role = "summary"
        if role == state["last_role"] and role != "compaction":
            chunks.append(f"\n{txt}\n")  # same speaker continues: extend the last message
            continue
        state["last_id"] += 1
        state["last_role"] = role
        new += 1
        chunks.append(f"\n## m{state['last_id']:04d} · {role} · {ts}\n{txt}\n")

    if not os.path.exists(log_path):
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"<!-- remember.me raw log | session: {session} -->\n")
    if chunks:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("".join(chunks))
    if summaries:
        with open(os.path.join(out_dir, f"{session}.compactions.md"), "a", encoding="utf-8") as f:
            for ts, txt in summaries:
                f.write(f"\n## compaction summary · {ts}\n{txt}\n")
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f)
    return new
