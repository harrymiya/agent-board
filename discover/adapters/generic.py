"""Adapters for agents that only need process-level monitoring."""

import re

from .base import AgentAdapter, DiscoveryContext, ProcessInfo


class GenericProcessAdapter(AgentAdapter):
    def __init__(self, name, patterns):
        self.name = name
        self._patterns = tuple(patterns)

    def matches(self, comm, cmdline):
        first = cmdline.split()[0].rsplit("/", 1)[-1] if cmdline.split() else ""
        return comm in self._patterns or first in self._patterns or any(
            re.search(rf"(?:^|/)({re.escape(name)})(?:\s|$)", cmdline)
            for name in self._patterns
        )

    def discover(self, context, processes):
        count = 0
        for process in processes:
            if process.kind == self.name:
                context.write_process(process, self.name)
                count += 1
        return count


class ClaudeAdapter(GenericProcessAdapter):
    def __init__(self):
        super().__init__("claude", ("claude",))


class DshAdapter(GenericProcessAdapter):
    def __init__(self):
        super().__init__("dsh", ("dsh",))

