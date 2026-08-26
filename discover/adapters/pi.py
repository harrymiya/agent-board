"""Pi process and JSONL session adapter."""

import json
import os
import re
import time

from .base import AgentAdapter, DiscoveryContext, process_cwd


def _text_from_message(message):
    content = (message or {}).get("content")
    if isinstance(content, list):
        return " ".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ).strip()
    return content.strip() if isinstance(content, str) else ""


def recent_think(pid, limit=5):
    """Read only the latest assistant text for the process' current project."""
    try:
        cwd = process_cwd(pid)
        if not cwd:
            return []
        slug = "--" + cwd.lstrip("/").rstrip("/").replace("/", "-") + "--"
        session_dir = os.path.expanduser(f"~/.pi/agent/sessions/{slug}")
        files = [os.path.join(session_dir, name) for name in os.listdir(session_dir)
                 if name.endswith(".jsonl")]
        if not files:
            return []
        path = max(files, key=os.path.getmtime)
        items = []
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if event.get("type") != "message":
                    continue
                message = event.get("message") or {}
                if message.get("role") != "assistant":
                    continue
                text = _text_from_message(message)
                if not text:
                    continue
                stamp = event.get("timestamp") or message.get("timestamp") or 0
                try:
                    hms = time.strftime("%H:%M:%S", time.localtime(float(stamp)))
                except (TypeError, ValueError, OverflowError):
                    hms = ""
                items.append((hms, text))
        return items[-limit:][::-1]
    except (OSError, ValueError):
        return []


class PiAdapter(AgentAdapter):
    name = "pi"

    def matches(self, comm, cmdline):
        first = cmdline.split()[0].rsplit("/", 1)[-1] if cmdline.split() else ""
        return (comm == "pi" or first == "pi" or
                bool(re.search(r"(?:^|/)pi(?:[\s-]|$)", cmdline)))

    def discover(self, context: DiscoveryContext, processes):
        count = 0
        for process in processes:
            if process.kind != self.name:
                continue
            context.write_process(process, self.name, think=recent_think(process.pid))
            count += 1
        return count
