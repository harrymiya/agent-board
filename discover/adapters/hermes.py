"""Hermes local session database adapter."""

import os
import sqlite3
import time

from .base import AgentAdapter, DiscoveryContext, ProcessInfo


STATE_DB = os.environ.get("AGENTBOARD_HERMES_STATE_DB", os.path.expanduser("~/.hermes/state.db"))


def _think(connection, session_id, limit=5):
    try:
        rows = connection.execute(
            "SELECT timestamp, reasoning, reasoning_content, content FROM messages "
            "WHERE session_id=? AND role='assistant' AND "
            "((reasoning IS NOT NULL AND length(reasoning)>0) OR "
            "(reasoning_content IS NOT NULL AND length(reasoning_content)>0) OR "
            "(content IS NOT NULL AND length(content)>0)) "
            "ORDER BY timestamp DESC LIMIT ?", (session_id, limit * 2)
        ).fetchall()
    except sqlite3.Error:
        return []
    result = []
    for stamp, reasoning, reasoning_content, content in rows:
        text = (reasoning or reasoning_content or content or "").strip()
        if not text:
            continue
        try:
            hms = time.strftime("%H:%M:%S", time.localtime(float(stamp)))
        except (TypeError, ValueError, OverflowError):
            hms = ""
        result.append((hms, text))
        if len(result) >= limit:
            break
    return result


def _subagent_label(connection, session_id, cwd, short):
    """Derive a human-readable task label for a sub-agent from its first user
    message (the delegated task), falling back to the working directory."""
    try:
        row = connection.execute(
            "SELECT content FROM messages WHERE session_id=? AND role='user' "
            "AND content IS NOT NULL AND length(content)>0 "
            "ORDER BY timestamp ASC LIMIT 1", (session_id,)
        ).fetchone()
    except sqlite3.Error:
        row = None
    if row and row[0]:
        text = " ".join(str(row[0]).split())
        if text:
            return text[:120]
    if cwd:
        return os.path.basename(cwd.rstrip("/")) or short
    return short


class HermesAdapter(AgentAdapter):
    name = "hermes"

    def matches(self, comm, cmdline):
        return "hermes_cli.main gateway run" in cmdline or (
            "venv/bin/hermes" in cmdline and "gateway" not in cmdline
        )

    _ACTIVE = "source IN ('cli','subagent') AND (ended_at IS NULL OR ended_at='') AND last_activity_at>?"

    def discover(self, context: DiscoveryContext, processes):
        # The gateway process itself is intentionally hidden; active CLI sessions
        # and their sub-agent sessions are represented by virtual cards with
        # their own independent summaries.  Sub-agents (source='subagent') get a
        # separate card named hermes-sub-<short> so delegated work is visible on
        # the board instead of being folded invisibly into the parent CLI card.
        count = 0
        try:
            with sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=2) as con:
                rows = con.execute(
                    "SELECT id, cwd, model, started_at, last_activity_at, "
                    "COALESCE(title,''), COALESCE(git_repo_root,''), source FROM sessions "
                    f"WHERE {self._ACTIVE} ORDER BY last_activity_at DESC",
                    (time.time() - 7200,)
                ).fetchall()
                for sid, cwd, model, started, _last, title, repo, source in rows:
                    short = sid.split("_", 2)[-1][:8] or sid
                    location = repo or cwd or ""
                    command = f"cwd={location} model={model}"
                    if source == "subagent":
                        label = _subagent_label(con, sid, cwd, short)
                        self._write_session(context, con, sid, short, started,
                                            command, label, prefix="hermes-sub")
                    else:
                        label = (title or "").strip() or os.path.basename(location) or short
                        self._write_session(context, con, sid, short, started,
                                            command, label, prefix="hermes")
                    count += 1
        except (OSError, sqlite3.Error):
            pass
        return count

    @staticmethod
    def _write_session(context, connection, session_id, short, started,
                       command, label, prefix="hermes"):
        agent = f"{prefix}-{short}"
        think = _think(connection, session_id)
        context.write_virtual(agent, started, command, "running", think, label)
