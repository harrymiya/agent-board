import importlib.util
import os
import tempfile
import time
import unittest


MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "server", "agentboard_server.py"
)
SPEC = importlib.util.spec_from_file_location("agentboard_server", MODULE_PATH)
SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER)


class BoardScannerStatusTest(unittest.TestCase):
    def _virtual_card(self, event_status):
        with tempfile.TemporaryDirectory() as root:
            scanner = SERVER.BoardScanner(root, "20260825")
            base = os.path.join(scanner.board_dir, "virtual-agent")
            with open(base + ".pid", "w", encoding="utf-8") as fh:
                fh.write("0")
            with open(base + ".start", "w", encoding="utf-8") as fh:
                fh.write(str(int(time.time())))
            with open(base + ".log", "w", encoding="utf-8") as fh:
                fh.write(f"12:00:00|active|{event_status}|task\n")
            return scanner.read_agent("virtual-agent")

    def test_live_session_preserves_done_task_state(self):
        card = self._virtual_card("done")
        self.assertEqual(card["status"], "running")
        self.assertEqual(card["last"]["status"], "done")
        self.assertEqual(card["cls"], {"group": "idle", "label": "DONE"})

    def test_live_session_preserves_waiting_task_state(self):
        card = self._virtual_card("waiting")
        self.assertEqual(card["last"]["status"], "waiting")
        self.assertEqual(card["cls"], {"group": "waiting", "label": "WAITING"})

    def test_unknown_task_state_falls_back_to_running(self):
        card = self._virtual_card("unknown-state")
        self.assertEqual(card["last"]["status"], "running")
        self.assertEqual(card["cls"], {"group": "running", "label": "RUNNING"})

    def test_idle_is_distinct_from_waiting(self):
        card = self._virtual_card("idle")
        self.assertEqual(card["last"]["status"], "idle")
        self.assertEqual(card["cls"], {"group": "idle", "label": "IDLE"})


if __name__ == "__main__":
    unittest.main()
