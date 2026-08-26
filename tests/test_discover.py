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


from discover.adapters.hermes import HermesAdapter, _subagent_label, _think
from discover.adapters.base import DiscoveryContext


class HermesSubAgentTest(unittest.TestCase):
    """Hermes sub-agent (source='subagent') sessions should appear as their own
    virtual cards, distinct from the parent CLI session card."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "state.db")
        con = sqlite3.connect(self.db)
        con.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, cwd TEXT, model TEXT, "
            "started_at REAL, last_activity_at REAL, ended_at REAL, title TEXT, "
            "git_repo_root TEXT, parent_session_id TEXT)"
        )
        con.execute(
            "CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT, "
            "reasoning TEXT, reasoning_content TEXT, timestamp REAL)"
        )
        now = time.time()
        con.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("20260826_000000_abc123", "cli", "/mnt/data/code", "dsv4",
             now - 3600, now - 10, None, "主任务", "", None),
        )
        con.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("20260826_000100_def456", "subagent", "/mnt/data/code", "dsv4",
             now - 600, now - 5, None, None, "", "20260826_000000_abc123"),
        )
        con.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("20260826_000200_ghi789", "subagent", "/mnt/data/code", "dsv4",
             now - 600, now - 5, now - 3, None, "", "20260826_000000_abc123"),
        )
        con.execute(
            "INSERT INTO messages VALUES (?,?,?,?,?,?)",
            ("20260826_000100_def456", "user", "请把 X 数据处理成 Y 格式并落库",
             None, None, now - 590),
        )
        con.execute(
            "INSERT INTO messages VALUES (?,?,?,?,?,?)",
            ("20260826_000100_def456", "assistant", "", "我在思考第一步如何做",
             None, now - 20),
        )
        con.commit()
        con.close()
        import discover.adapters.hermes as hermes_mod
        self.hermes_mod = hermes_mod
        self.original_state_db = hermes_mod.STATE_DB
        hermes_mod.STATE_DB = self.db

    def tearDown(self):
        self.hermes_mod.STATE_DB = self.original_state_db
        self.tmp.cleanup()

    def test_active_subagent_gets_own_card(self):
        out_dir = os.path.join(self.tmp.name, "out")
        os.makedirs(out_dir, exist_ok=True)
        context = DiscoveryContext(self.tmp.name, "20260826", out_dir)
        count = HermesAdapter().discover(context, [])
        files = set(os.listdir(out_dir))
        # parent and active sub-agent cards; ended sub-agent excluded
        self.assertEqual(count, 2)
        self.assertIn("hermes-abc123.pid", files)
        self.assertIn("hermes-sub-def456.pid", files)
        self.assertNotIn("hermes-sub-ghi789.pid", files)
        self.assertTrue(os.path.isfile(os.path.join(out_dir, "hermes-sub-def456.think")))
        with open(os.path.join(out_dir, "hermes-sub-def456.think"),
                  encoding="utf-8") as fh:
            think = fh.read()
        self.assertIn("我在思考第一步如何做", think)

    def test_subagent_label_from_first_user_message(self):
        con = sqlite3.connect(self.db)
        label = _subagent_label(con, "20260826_000100_def456", "/mnt/data/code", "def456")
        con.close()
        self.assertEqual(label, "请把 X 数据处理成 Y 格式并落库")


class PrimeTimestampParseTest(unittest.TestCase):
    """prime parse_session must handle ISO and epoch timestamps (was float() on
    ISO strings -> all think/status timestamps came back empty, which broke the
    board's "recent think -> running" freshness check)."""

    def test_iso_timestamp_parses(self):
        from discover.adapters.prime import _ts_to_epoch, _ts_hms
        epoch = _ts_to_epoch("2026-08-26T04:19:57.717Z")
        self.assertIsNotNone(epoch)
        self.assertAlmostEqual(epoch, 1787717997.717, delta=1.5)
        self.assertRegex(_ts_hms("2026-08-26T04:19:57.717Z"), r"^\d{2}:\d{2}:\d{2}$")

    def test_epoch_and_millis_parse(self):
        from discover.adapters.prime import _ts_to_epoch
        self.assertAlmostEqual(_ts_to_epoch(1787717995.7), 1787717995.7)
        # milliseconds epoch
        self.assertAlmostEqual(_ts_to_epoch(1787717995457), 1787717995.457, delta=1.5)

    def test_assistant_message_timestamp_populated_in_think(self):
        import json
        from discover.adapters.prime import parse_session
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".jsonl", delete=False) as fh:
            fh.write('{"type":"session","id":"s1","timestamp":"2026-08-26T04:19:42.138Z"}\n')
            fh.write('{"type":"message","timestamp":"2026-08-26T04:19:57.717Z",'
                     '"message":{"role":"assistant","content":[{"type":"text","text":"hello thought"}]}}\n')
            name = fh.name
        try:
            info = parse_session(name)
        finally:
            os.unlink(name)
        self.assertEqual(len(info["think"]), 1)
        hms, text = info["think"][0]
        self.assertEqual(text, "hello thought")
        self.assertRegex(hms, r"^\d{2}:\d{2}:\d{2}$")


if __name__ == "__main__":
    unittest.main()
