import importlib.util
import os
import sqlite3
import tempfile
import time
import unittest


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


if __name__ == "__main__":
    unittest.main()
