import importlib.util
import os
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from discover.adapters.codex import CodexAdapter
from discover.adapters.processes_macos import parse_ps_line, scan_processes as scan_macos_processes
from discover.adapters.registry import load_adapters


MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "discover", "hermes_board_discover.py"
)
SPEC = importlib.util.spec_from_file_location("hermes_board_discover", MODULE_PATH)
DISCOVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DISCOVER)


class CodexServerActivityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "logs.sqlite")
        con = sqlite3.connect(self.db)
        con.execute(
            "CREATE TABLE logs (ts INTEGER, level TEXT, target TEXT, "
            "feedback_log_body TEXT, process_uuid TEXT)"
        )
        now = int(time.time())
        con.executemany(
            "INSERT INTO logs VALUES (?, 'INFO', 'codex', ?, ?)",
            [
                (now, "pid one active", "pid:101:one"),
                (now - 120, "pid two stale", "pid:202:two"),
            ],
        )
        con.commit()
        con.close()
        self.original_db = DISCOVER.CODEX_LOG_DB
        DISCOVER.CODEX_LOG_DB = self.db

    def tearDown(self):
        DISCOVER.CODEX_LOG_DB = self.original_db
        self.tmp.cleanup()

    def test_activity_is_scoped_to_process_pid(self):
        _, active_items, active = DISCOVER._codex_server_activity(101)
        _, stale_items, stale = DISCOVER._codex_server_activity(202)
        self.assertTrue(active)
        self.assertFalse(stale)
        self.assertEqual(active_items[0][1], "pid one active")
        self.assertEqual(stale_items[0][1], "pid two stale")

    def test_unknown_pid_is_waiting(self):
        last_hms, items, active = DISCOVER._codex_server_activity(303)
        self.assertIsNone(last_hms)
        self.assertEqual(items, [])
        self.assertFalse(active)


class PrimeProcessStatusTest(unittest.TestCase):
    def _status(self, task_state):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as session:
            session.write(
                '{"type":"agent_status","status":{"taskState":"%s"}}\n'
                % task_state
            )
            session.flush()
            original = DISCOVER._prime_session_for_pid
            DISCOVER._prime_session_for_pid = lambda _pid: session.name
            try:
                return DISCOVER._prime_process_status(101)
            finally:
                DISCOVER._prime_session_for_pid = original

    def test_needs_input_is_waiting(self):
        self.assertEqual(self._status("needs_input"), "waiting")

    def test_completed_is_done(self):
        self.assertEqual(self._status("completed"), "done")

    def test_unknown_state_is_idle(self):
        self.assertEqual(self._status("mystery"), "idle")


class DiscoverOutputTest(unittest.TestCase):
    def test_main_replaces_output_directory_atomically(self):
        with tempfile.TemporaryDirectory() as root:
            final_out = os.path.join(root, "20260825", "__discovered__")
            os.makedirs(final_out)
            with open(os.path.join(final_out, "old.pid"), "w", encoding="utf-8") as fh:
                fh.write("123")

            original = {
                "out": DISCOVER.OUT_DIR,
                "scan": DISCOVER.scan_processes,
                "adapters": DISCOVER.load_adapters,
            }
            DISCOVER.OUT_DIR = final_out
            DISCOVER.scan_processes = lambda _adapters: []

            class FakeAdapter:
                name = "fake"

                def discover(self, context, _processes):
                    with open(os.path.join(context.out_dir, "new.pid"), "w", encoding="utf-8") as fh:
                        fh.write("456")
                    return 1

            DISCOVER.load_adapters = lambda: [FakeAdapter()]
            try:
                DISCOVER.main()
            finally:
                DISCOVER.OUT_DIR = original["out"]
                DISCOVER.scan_processes = original["scan"]
                DISCOVER.load_adapters = original["adapters"]

            self.assertTrue(os.path.isfile(os.path.join(final_out, "new.pid")))
            self.assertFalse(os.path.exists(os.path.join(final_out, "old.pid")))


class AdapterRegistryTest(unittest.TestCase):
    def test_builtin_adapters_are_independently_registered(self):
        names = [adapter.name for adapter in load_adapters()]
        self.assertEqual(names[:5], ["hermes", "prime", "codex", "opencode", "pi"])
        self.assertIn("claude", names)
        self.assertIn("dsh", names)

    def test_macos_ps_line_preserves_process_start_time(self):
        parsed = parse_ps_line(
            " 4242 codex Wed Aug 26 12:34:56 2026 /usr/local/bin/codex exec"
        )
        self.assertEqual(parsed[0], 4242)
        self.assertEqual(parsed[1], "codex")
        self.assertIn("codex exec", parsed[2])
        self.assertGreater(parsed[3], 0)

    def test_macos_process_provider_uses_registered_adapters(self):
        result = SimpleNamespace(
            returncode=0,
            stdout=" 4242 codex Wed Aug 26 12:34:56 2026 /usr/local/bin/codex exec\n",
        )
        with patch("discover.adapters.processes_macos.subprocess.run", return_value=result):
            processes = scan_macos_processes([CodexAdapter()], current_pid=9999)
        self.assertEqual(len(processes), 1)
        self.assertEqual(processes[0].kind, "codex")
        self.assertEqual(processes[0].start_epoch_override, parse_ps_line(result.stdout)[3])


if __name__ == "__main__":
    unittest.main()
