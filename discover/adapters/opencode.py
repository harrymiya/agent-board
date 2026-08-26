"""OpenCode SQLite session adapter."""

import json
import os
import re
import sqlite3
import time

from .base import AgentAdapter, DiscoveryContext


DB_PATH = os.path.expanduser("~/.local/share/opencode/opencode.db")


def _text_think(session_id, limit):
    try:
        with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=3) as con:
            rows = con.execute(
                "SELECT time_created, data FROM part "
                "WHERE session_id=? AND json_extract(data,'$.type')='text' "
                "AND json_extract(data,'$.text') IS NOT NULL "
                "AND length(json_extract(data,'$.text'))>20 "
                "AND time_created>? ORDER BY time_created DESC LIMIT ?",
                (session_id, int(time.time() * 1000) - 86400 * 2 * 1000, limit),
            ).fetchall()
        return _rows_to_think(rows, limit)
    except (OSError, sqlite3.Error):
        return []


def _rows_to_think(rows, limit):
    result = []
    for stamp, raw in rows:
        try:
            text = (json.loads(raw).get("text") or "").strip()
            hms = time.strftime("%H:%M:%S", time.localtime(stamp / 1000))
        except (TypeError, ValueError, AttributeError, json.JSONDecodeError):
            continue
        if text:
            result.append((hms, text))
        if len(result) >= limit:
            break
    return result


def recent_think(pid, limit=5):
    if not os.path.exists(DB_PATH):
        return []
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
        with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=3) as con:
            sessions = con.execute(
                "SELECT id, directory FROM session "
                "WHERE time_archived IS NULL ORDER BY time_updated DESC LIMIT 15"
            ).fetchall()
            best = None
            best_length = -1
            for session_id, directory in sessions:
                directory = (directory or "").rstrip("/")
                if directory and (cwd == directory or cwd.startswith(directory + "/")):
                    if len(directory) > best_length:
                        best, best_length = session_id, len(directory)
            if not best:
                return []
            rows = con.execute(
                "SELECT time_created, data FROM part "
                "WHERE session_id=? AND json_extract(data,'$.type')='reasoning' "
                "AND json_extract(data,'$.text') IS NOT NULL "
                "AND length(json_extract(data,'$.text'))>0 "
                "AND time_created>? ORDER BY time_created DESC LIMIT ?",
                (best, int(time.time() * 1000) - 86400 * 2 * 1000, limit * 3),
            ).fetchall()
        return _rows_to_think(rows, limit) if rows else _text_think(best, limit)
    except (OSError, sqlite3.Error):
        return []


class OpenCodeAdapter(AgentAdapter):
    name = "opencode"

    def matches(self, comm, cmdline):
        first = cmdline.split()[0].rsplit("/", 1)[-1] if cmdline.split() else ""
        return (comm == self.name or first == self.name or
                bool(re.search(r"(?:^|/)opencode(?:\s|$)", cmdline)))

    def discover(self, context: DiscoveryContext, processes):
        count = 0
        for process in processes:
            if process.kind != self.name:
                continue
            context.write_process(process, self.name, think=recent_think(process.pid))
            count += 1
        return count
