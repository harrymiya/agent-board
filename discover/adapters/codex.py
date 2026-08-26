"""Codex process, rollout and app-server adapters."""

import ast
import json
import os
import re
import sqlite3
import time

from .base import AgentAdapter, DiscoveryContext, ProcessInfo, process_cwd


LOG_DB = os.environ.get("AGENTBOARD_CODEX_LOG_DB", os.path.expanduser("~/.codex/logs_2.sqlite"))
ROLLOUTS = os.path.expanduser("~/.codex/sessions")
ACTIVE_WINDOW = 15.0


def _payload(event):
    try:
        value = event.get("payload")
        return ast.literal_eval(value) if isinstance(value, str) else value
    except (ValueError, SyntaxError, TypeError):
        return None


def _message_text(message):
    content = (message or {}).get("content")
    if not isinstance(content, list):
        return ""
    return " ".join(
        part.get("text", "") for part in content
        if isinstance(part, dict) and part.get("text")
    ).strip()


def server_activity(pid, limit=3):
    """Return latest app-server activity scoped to one process UUID."""
    if not os.path.exists(LOG_DB):
        return None, [], False
    try:
        pattern = f"pid:{int(pid)}:%"
        with sqlite3.connect(f"file:{LOG_DB}?mode=ro", uri=True, timeout=2) as con:
            row = con.execute("SELECT MAX(ts) FROM logs WHERE process_uuid LIKE ?", (pattern,)).fetchone()
            last_epoch = float(row[0]) if row and row[0] else None
            rows = con.execute(
                "SELECT ts, feedback_log_body FROM logs WHERE level='INFO' "
                "AND feedback_log_body IS NOT NULL AND trim(feedback_log_body) != '' "
                "AND target NOT LIKE '%otel%' AND target NOT LIKE '%feedback_tags%' "
                "AND target NOT LIKE '%custom_ca%' AND process_uuid LIKE ? "
                "ORDER BY ts DESC LIMIT 20", (pattern,)
            ).fetchall()
        items = []
        seen = set()
        for stamp, body in rows:
            text = " ".join((body or "").split())[:160]
            if not text or text[:60] in seen:
                continue
            seen.add(text[:60])
            try:
                hms = time.strftime("%H:%M:%S", time.localtime(float(stamp)))
            except (TypeError, ValueError, OverflowError):
                hms = ""
            items.append((hms, text))
            if len(items) >= limit:
                break
        last_hms = time.strftime("%H:%M:%S", time.localtime(last_epoch)) if last_epoch else None
        active = last_epoch is not None and time.time() - last_epoch <= ACTIVE_WINDOW
        return last_hms, items, active
    except (OSError, ValueError, sqlite3.Error):
        return None, [], False


def _rollout_for_cwd(cwd):
    if not cwd or not os.path.isdir(ROLLOUTS):
        return None
    cwd = os.path.realpath(cwd)
    best = None
    best_score = -1
    best_mtime = 0
    try:
        for root, _, names in os.walk(ROLLOUTS):
            for name in names:
                if not name.startswith("rollout-") or not name.endswith(".jsonl"):
                    continue
                path = os.path.join(root, name)
                rollout_cwd = ""
                try:
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            try:
                                event = json.loads(line)
                            except (TypeError, ValueError):
                                continue
                            if event.get("type") == "session_meta":
                                rollout_cwd = (_payload(event) or {}).get("cwd", "")
                                break
                except OSError:
                    continue
                rollout_cwd = os.path.realpath(rollout_cwd) if rollout_cwd else ""
                if rollout_cwd == cwd:
                    score = 1000
                elif cwd.startswith(rollout_cwd + os.sep) or rollout_cwd.startswith(cwd + os.sep):
                    score = min(len(cwd), len(rollout_cwd))
                else:
                    continue
                mtime = os.path.getmtime(path)
                if score > best_score or (score == best_score and mtime > best_mtime):
                    best, best_score, best_mtime = path, score, mtime
    except OSError:
        return None
    return best


def rollout_think(pid, limit=5):
    try:
        cwd = process_cwd(pid)
        path = _rollout_for_cwd(cwd) if cwd else None
        if not path:
            return []
        items = []
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    event = json.loads(line)
                    payload = _payload(event)
                except (TypeError, ValueError):
                    continue
                if event.get("type") != "response_item" or not isinstance(payload, dict):
                    continue
                if payload.get("type") != "message" or payload.get("role") != "assistant":
                    continue
                text = _message_text(payload)
                if not text:
                    continue
                stamp = event.get("timestamp") or 0
                try:
                    sort_key = float(stamp)
                    hms = time.strftime("%H:%M:%S", time.localtime(sort_key))
                except (TypeError, ValueError, OverflowError):
                    sort_key, hms = 0, ""
                items.append((sort_key, hms, text))
        return [(hms, text) for _, hms, text in sorted(items)[-limit:][::-1]]
    except OSError:
        return []


class CodexAdapter(AgentAdapter):
    name = "codex"

    def matches(self, comm, cmdline):
        first = cmdline.split()[0].rsplit("/", 1)[-1] if cmdline.split() else ""
        return (comm in ("codex", "codexjs") or first in ("codex", "codexjs") or
                bool(re.search(r"(?:^|/)codex(?:\s|$)", cmdline)))

    def discover(self, context: DiscoveryContext, processes):
        count = 0
        for process in processes:
            if process.kind != self.name:
                continue
            first = process.cmdline.split()[0].rsplit("/", 1)[-1] if process.cmdline.split() else ""
            if first == "node":
                continue
            app_server = "app-server" in process.cmdline
            events = []
            think = []
            status = "running"
            if app_server:
                last_hms, entries, active = server_activity(process.pid)
                status = "running" if active else "idle"
                if last_hms:
                    events.append((last_hms, "active", status,
                                   entries[0][1] if entries else "(no recent INFO log)"))
                think = entries
            else:
                think = rollout_think(process.pid)
                if think:
                    events.append((think[0][0], "active", "running", think[0][1]))
            context.write_process(process, self.name, status=status, think=think, events=events)
            count += 1
        return count
