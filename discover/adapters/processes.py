"""Portable /proc process collection used by all adapters."""

import os
import re
from typing import Iterable, List, Sequence

from .base import AgentAdapter, ProcessInfo


def _boot_time():
    try:
        with open("/proc/stat", encoding="ascii", errors="ignore") as fh:
            match = re.search(r"^btime\s+(\d+)$", fh.read(), re.MULTILINE)
            return int(match.group(1)) if match else None
    except OSError:
        return None


def scan_processes(adapters: Sequence[AgentAdapter]) -> List[ProcessInfo]:
    """Read /proc once and let adapters decide ownership of each process."""
    found: List[ProcessInfo] = []
    current_pid = os.getpid()
    boot_time = _boot_time()
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == current_pid:
            continue
        pid = int(entry)
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmdline = fh.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
            with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
                stat = fh.read()
            with open(f"/proc/{pid}/comm", encoding="utf-8", errors="replace") as fh:
                comm = fh.read().strip()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if not cmdline:
            continue
        tokens = stat.rsplit(")", 1)[-1].split() if ")" in stat else []
        try:
            start_ticks = int(tokens[19])
        except (ValueError, IndexError):
            start_ticks = 0
        for adapter in adapters:
            if adapter.matches(comm, cmdline):
                found.append(ProcessInfo(pid, comm, cmdline, start_ticks, boot_time, adapter.name))
                break
    return found
