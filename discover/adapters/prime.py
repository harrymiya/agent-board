"""Prime worker and sub-agent adapter."""

import json
import os
import re
import time

from .base import AgentAdapter, DiscoveryContext, ProcessInfo, process_alive, read_jsonl


LEASES = os.path.expanduser("~/.prime/agent/session-leases")
ARTIFACTS = os.path.expanduser("~/.prime/agent/session-artifacts")


def session_for_pid(pid):
    candidates = []
    try:
        for lock_dir in os.listdir(LEASES):
            directory = os.path.join(LEASES, lock_dir)
            if not os.path.isdir(directory):
                continue
            for filename in os.listdir(directory):
                path = os.path.join(directory, filename)
                try:
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        lease = json.load(fh)
                except (OSError, TypeError, ValueError):
                    continue
                if isinstance(lease, dict) and lease.get("pid") == pid and lease.get("sessionPath"):
                    session_path = str(lease["sessionPath"])
                    candidates.append(("sessions/" in session_path,
                                       str(lease.get("createdAt") or ""),
                                       os.path.expanduser(session_path)))
    except OSError:
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][2]


def _message_text(event):
    message = event.get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        return " ".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and part.get("type") in ("text", "output_text")
        ).strip()
    return content.strip() if isinstance(content, str) else ""


def _ts_to_epoch(value):
    """Normalize a session-event timestamp to seconds-epoch (float) or None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000.0 if number > 10_000_000_000 else number
    raw = str(value).strip()
    if not raw:
        return None
    try:
        number = float(raw)
        return number / 1000.0 if number > 10_000_000_000 else number
    except ValueError:
        pass
    text = raw.replace("Z", "+00:00")
    try:
        from datetime import datetime
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return parsed.timestamp()
    except ValueError:
        return None


def _ts_hms(value):
    """Format a session-event timestamp as HH:MM:SS ('' when unparseable)."""
    epoch = _ts_to_epoch(value)
    if epoch is None:
        return ""
    try:
        return time.strftime("%H:%M:%S", time.localtime(epoch))
    except (OverflowError, OSError, ValueError):
        return ""


def parse_session(path):
    info = {"session_id": "", "start": None, "task": "", "think": [],
            "task_state": "", "last_hms": ""}
    messages = []
    for event in read_jsonl(path):
        event_type = event.get("type")
        if event_type == "session":
            info["session_id"] = event.get("id") or info["session_id"]
            if info["start"] is None:
                info["start"] = _ts_to_epoch(event.get("timestamp"))
        elif event_type == "custom_message":
            content = event.get("content") or ""
            if "[task from parent]" in content and not info["task"]:
                info["task"] = content.replace("[task from parent]", "").strip()
        elif event_type == "agent_status":
            state = (event.get("status") or {}).get("taskState") or ""
            if state:
                info["task_state"] = str(state).strip().lower()
            info["last_hms"] = _ts_hms(event.get("timestamp"))
        elif event_type == "message" and (event.get("message") or {}).get("role") == "assistant":
            text = _message_text(event)
            if not text:
                continue
            stamp = _ts_to_epoch(event.get("timestamp")) or 0.0
            hms = _ts_hms(event.get("timestamp"))
            messages.append((stamp, hms, text))
    info["think"] = [(hms, text) for _, hms, text in sorted(messages)[-5:][::-1]]
    return info


def process_status(pid):
    path = session_for_pid(pid)
    if not path or not os.path.isfile(path):
        return "idle"
    state = parse_session(path).get("task_state", "")
    if state in ("completed", "done", "success", "finished"):
        return "done"
    if state in ("needs_input", "waiting", "awaiting", "blocked_on", "on_hold"):
        return "waiting"
    if state in ("thinking", "think"):
        return "thinking"
    if state in ("running", "working", "active", "started"):
        return "running"
    return "idle"


def _alive(pid):
    return process_alive(pid)


def _parent_pid(prefix):
    try:
        for lock_dir in os.listdir(LEASES):
            directory = os.path.join(LEASES, lock_dir)
            if not os.path.isdir(directory):
                continue
            for filename in os.listdir(directory):
                try:
                    with open(os.path.join(directory, filename), encoding="utf-8", errors="replace") as fh:
                        lease = json.load(fh)
                except (OSError, TypeError, ValueError):
                    continue
                if prefix in str(lease.get("sessionPath") or ""):
                    return lease.get("pid")
    except OSError:
        pass
    return None


def _subagents():
    result = []
    try:
        for parent in sorted(os.listdir(ARTIFACTS)):
            parent_dir = os.path.join(ARTIFACTS, parent)
            if not os.path.isdir(parent_dir):
                continue
            owner_pid = _parent_pid(parent)
            if owner_pid is None or not _alive(owner_pid):
                continue
            for sub in sorted(os.listdir(parent_dir)):
                if not sub.startswith("sub-"):
                    continue
                sub_dir = os.path.join(parent_dir, sub)
                files = [os.path.join(sub_dir, name) for name in os.listdir(sub_dir)
                         if name.endswith(".jsonl")] if os.path.isdir(sub_dir) else []
                if not files:
                    continue
                info = parse_session(max(files, key=os.path.getmtime))
                if not info["session_id"]:
                    continue
                status = info["task_state"]
                meta = os.path.join(sub_dir, "rlm-subagent.json")
                try:
                    with open(meta, encoding="utf-8", errors="replace") as fh:
                        status = str(json.load(fh).get("status") or status).lower()
                except (OSError, TypeError, ValueError):
                    pass
                if status in ("completed", "done", "success", "finished"):
                    status = "done"
                elif status in ("needs_input", "waiting", "awaiting"):
                    status = "waiting"
                else:
                    status = "running"
                result.append({"agent": f"prime-sub-{info['session_id'][-8:]}",
                               "start": info["start"], "task": info["task"],
                               "think": info["think"], "status": status,
                               "last_hms": info["last_hms"], "pid": owner_pid})
    except OSError:
        pass
    return result


class PrimeAdapter(AgentAdapter):
    name = "prime"

    def matches(self, comm, cmdline):
        first = cmdline.split()[0].rsplit("/", 1)[-1] if cmdline.split() else ""
        return (comm == "prime-agent" or first == "prime-agent" or
                bool(re.search(r"(?:^|/)prime-agent(?:\s|$)", cmdline)))

    def discover(self, context: DiscoveryContext, processes):
        count = 0
        for process in processes:
            if process.kind != self.name:
                continue
            path = session_for_pid(process.pid)
            info = parse_session(path) if path and os.path.isfile(path) else {}
            context.write_process(process, self.name,
                                  status=process_status(process.pid),
                                  think=info.get("think", []))
            count += 1
        for sub in _subagents():
            task = " ".join((sub["task"] or "(prime sub-agent)").split())[:120]
            context.write_virtual(sub["agent"], sub["start"], task,
                                  sub["status"], sub["think"], task)
            count += 1
        return count
