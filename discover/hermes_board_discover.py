#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent Board discovery coordinator.

This file intentionally contains orchestration only. Agent-specific matching
and data parsing lives in ``discover/adapters/`` and can be extended with
``AGENTBOARD_ADAPTERS=your_package.adapter``.
"""

import os
import shutil
import sys
import tempfile
import time

try:  # package import (tests/embedders)
    from .adapters.base import DiscoveryContext, first_lines
    from .adapters.codex import server_activity as _codex_activity
    from .adapters.prime import process_status as _prime_status
    from .adapters.prime import session_for_pid as _prime_session
    from .adapters.processes import scan_processes as _scan_processes
    from .adapters.registry import load_adapters
except ImportError:  # direct ``python discover/hermes_board_discover.py``
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from adapters.base import DiscoveryContext, first_lines
    from adapters.codex import server_activity as _codex_activity
    from adapters.prime import process_status as _prime_status
    from adapters.prime import session_for_pid as _prime_session
    from adapters.processes import scan_processes as _scan_processes
    from adapters.registry import load_adapters


BOARD_ROOT = os.environ.get("AGENTBOARD_ROOT", os.path.expanduser("~/.hermes/agent-board"))
RUN = os.environ.get("AGENTBOARD_RUN", time.strftime("%Y%m%d"))
OUT_DIR = os.path.join(BOARD_ROOT, RUN, "__discovered__")

# Compatibility names retained for callers that used the old helper API.
CODEX_LOG_DB = os.environ.get("AGENTBOARD_CODEX_LOG_DB", os.path.expanduser("~/.codex/logs_2.sqlite"))


def scan_processes(adapters=None):
    """Scan processes through the registered adapters."""
    return _scan_processes(adapters or load_adapters())


def write_think_file(base, entries):
    """Compatibility helper for older integrations."""
    with open(base + ".think", "w", encoding="utf-8") as fh:
        for hms, text in list(entries)[:5]:
            for line in first_lines(text, 1, 160):
                fh.write(f"{hms}|{' '.join(line.split())[:160]}\n")


def _codex_server_activity(pid=None, limit=2):
    """Compatibility wrapper around the Codex plugin."""
    try:
        from .adapters import codex
    except ImportError:
        from adapters import codex

    old = codex.LOG_DB
    codex.LOG_DB = CODEX_LOG_DB
    try:
        return _codex_activity(pid, limit)
    finally:
        codex.LOG_DB = old


def _prime_session_for_pid(pid):
    return _prime_session(pid)


def _prime_process_status(pid):
    """Compatibility wrapper that keeps old monkey-patching behavior working."""
    try:
        from .adapters import prime
    except ImportError:
        from adapters import prime

    old = prime.session_for_pid
    prime.session_for_pid = _prime_session_for_pid
    try:
        return _prime_status(pid)
    finally:
        prime.session_for_pid = old


def _publish(staging, final_out):
    """Publish complete card files without exposing partially written files."""
    os.makedirs(final_out, exist_ok=True)
    staged_names = set(os.listdir(staging))
    for name in staged_names:
        os.replace(os.path.join(staging, name), os.path.join(final_out, name))
    for name in os.listdir(final_out):
        if name not in staged_names:
            try:
                os.remove(os.path.join(final_out, name))
            except OSError:
                pass
    os.rmdir(staging)


def main():
    final_out = OUT_DIR
    run_dir = os.path.dirname(final_out)
    os.makedirs(run_dir, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".discovered-", dir=run_dir)
    try:
        adapters = load_adapters()
        context = DiscoveryContext(BOARD_ROOT, RUN, staging)
        processes = scan_processes(adapters)
        counts = {}
        for adapter in adapters:
            try:
                counts[adapter.name] = adapter.discover(context, processes)
            except Exception as exc:
                # One vendor integration must not hide all other agents.
                counts[adapter.name] = 0
                print(f"agentboard: adapter {adapter.name!r} failed: {type(exc).__name__}: {exc}")
        _publish(staging, final_out)
        staging = None
        process_count = sum(counts.get(name, 0) for name in ("codex", "claude", "opencode", "pi", "prime", "dsh"))
        print(
            f"discovered: sessions={counts.get('hermes', 0)} "
            f"procs={process_count} adapters={len(adapters)} "
            f"({', '.join(f'{key}:{value}' for key, value in sorted(counts.items()) if value) or 'none'}) "
            f"→ {final_out}"
        )
    finally:
        if staging and os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    main()
