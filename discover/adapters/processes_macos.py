"""macOS process provider based on the system ``ps`` command.

The adapter deliberately keeps the output identical to the Linux /proc
provider so Agent-specific plugins do not need platform branches.
"""

import datetime as dt
import re
import subprocess
from typing import List, Sequence

from .base import AgentAdapter, ProcessInfo


PS_DATE = "%a %b %d %H:%M:%S %Y"
PS_LINE = re.compile(r"^\s*(\d+)\s+(\S+)\s+(.*)$")


def parse_ps_line(line: str):
    """Parse one ``ps -axo pid=,comm=,lstart=,args=`` line."""
    match = PS_LINE.match(line)
    if not match:
        return None
    pid, comm, tail = match.groups()
    fields = tail.split(None, 5)
    if len(fields) < 6:
        return None
    try:
        started = int(dt.datetime.strptime(" ".join(fields[:5]), PS_DATE).timestamp())
        return int(pid), comm, fields[5], started
    except (TypeError, ValueError, OverflowError):
        return None


def scan_processes(adapters: Sequence[AgentAdapter], current_pid: int):
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,comm=,lstart=,args="],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    found: List[ProcessInfo] = []
    for line in result.stdout.splitlines():
        parsed = parse_ps_line(line)
        if not parsed:
            continue
        pid, comm, cmdline, started = parsed
        if pid == current_pid or not cmdline:
            continue
        for adapter in adapters:
            if adapter.matches(comm, cmdline):
                found.append(ProcessInfo(
                    pid=pid, comm=comm, cmdline=cmdline, start_ticks=0,
                    boot_time=None, kind=adapter.name,
                    start_epoch_override=started,
                ))
                break
    return found

