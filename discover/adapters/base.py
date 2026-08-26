"""Small plugin API shared by Agent Board discovery adapters."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple


ThinkEntry = Tuple[str, str]


@dataclass(frozen=True)
class ProcessInfo:
    """A process snapshot collected once by the core scanner."""

    pid: int
    comm: str
    cmdline: str
    start_ticks: int
    boot_time: Optional[int]
    kind: str = ""
    start_epoch_override: Optional[int] = None

    @property
    def start_epoch(self) -> int:
        if self.start_epoch_override is not None:
            return self.start_epoch_override
        if self.boot_time is None:
            return int(time.time())
        try:
            hz = os.sysconf("SC_CLK_TCK")
        except (ValueError, OSError, AttributeError):
            hz = 100
        return int(self.boot_time + self.start_ticks / hz)


@dataclass
class DiscoveryContext:
    """Runtime services exposed to adapters without sharing global state."""

    root: str
    run: str
    out_dir: str
    now: float = field(default_factory=time.time)

    def path(self, agent: str, suffix: str) -> str:
        return os.path.join(self.out_dir, f"{agent}{suffix}")

    def write_text(self, path: str, value: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(value)

    def write_think(self, agent: str, entries: Iterable[ThinkEntry], limit: int = 5) -> None:
        lines: List[str] = []
        for hms, text in list(entries)[:limit]:
            for line in first_lines(text, 1, 160):
                lines.append(f"{hms}|{' '.join(line.split())[:160]}\n")
        self.write_text(self.path(agent, ".think"), "".join(lines))

    def write_process(
        self,
        process: ProcessInfo,
        label: str,
        status: str = "running",
        think: Sequence[ThinkEntry] = (),
        events: Sequence[Tuple[str, str, str, str]] = (),
    ) -> str:
        """Materialize one process card in the legacy board-file format."""
        agent = f"{label}-{process.pid}"
        base = os.path.join(self.out_dir, agent)
        self.write_text(base + ".pid", str(process.pid))
        self.write_text(base + ".start", str(process.start_epoch))
        self.write_text(base + ".cmd", " ".join(process.cmdline.split())[:200])

        log_lines = [
            f"{time.strftime('%H:%M:%S')}|launched|{status}|pid={process.pid} :: "
            f"{' '.join(process.cmdline.split())[:90]}\n"
        ]
        for ts, stage, event_status, message in events:
            log_lines.append(f"{ts}|{stage}|{event_status}|{message[:160]}\n")
        self.write_text(base + ".log", "".join(log_lines))
        if think:
            self.write_think(agent, think)
        return agent

    def write_virtual(
        self,
        agent: str,
        start: float,
        command: str,
        status: str = "running",
        think: Sequence[ThinkEntry] = (),
        message: str = "",
    ) -> None:
        """Materialize a session card without an OS process (pid=0)."""
        base = os.path.join(self.out_dir, agent)
        self.write_text(base + ".pid", "0")
        self.write_text(base + ".start", str(int(start or self.now)))
        self.write_text(base + ".cmd", command[:240])
        label = message.replace("|", "/")[:130] or agent
        ts = time.strftime("%H:%M:%S")
        self.write_text(base + ".log", f"{ts}|running|{status}|{label}\n")
        self.write_think(agent, think)


class AgentAdapter:
    """Base class for built-in and third-party adapters."""

    name = "adapter"

    def matches(self, comm: str, cmdline: str) -> bool:
        return False

    def discover(self, context: DiscoveryContext, processes: Sequence[ProcessInfo]) -> int:
        return 0


def first_lines(text: str, count: int = 2, max_length: int = 90) -> List[str]:
    lines: List[str] = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        lines.append(line[:max_length])
        if len(lines) >= count:
            break
    return lines


def read_jsonl(path: str):
    import json

    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    value = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(value, dict):
                    yield value
    except OSError:
        return


def process_cwd(pid: int) -> Optional[str]:
    """Resolve a process working directory on Linux and macOS."""
    try:
        if os.path.isdir("/proc"):
            return os.readlink(f"/proc/{pid}/cwd")
        result = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        for line in result.stdout.splitlines():
            if line.startswith("n"):
                return line[1:]
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def process_alive(pid: int) -> bool:
    """Check liveness without assuming Linux procfs."""
    try:
        if os.path.isdir("/proc"):
            with open(f"/proc/{int(pid)}/stat", encoding="utf-8", errors="replace") as fh:
                fields = fh.read().rsplit(")", 1)[-1].split()
            if not fields or fields[0] == "Z":
                return False
        os.kill(int(pid), 0)
        return True
    except (OSError, ProcessLookupError, ValueError, IndexError):
        return False
