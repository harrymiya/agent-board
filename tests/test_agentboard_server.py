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


    def _virtual_card_with_think(self, event_status, think_ts):
        with tempfile.TemporaryDirectory() as root:
            scanner = SERVER.BoardScanner(root, "20260825")
            base = os.path.join(scanner.board_dir, "virtual-agent")
            with open(base + ".pid", "w", encoding="utf-8") as fh:
                fh.write("0")
            with open(base + ".start", "w", encoding="utf-8") as fh:
                fh.write(str(int(time.time())))
            with open(base + ".log", "w", encoding="utf-8") as fh:
                fh.write(f"12:00:00|active|{event_status}|task\n")
            with open(base + ".think", "w", encoding="utf-8") as fh:
                fh.write(f"{think_ts}|recent thinking line\n")
            return scanner.read_agent("virtual-agent")

    def test_idle_promotes_to_running_with_fresh_thinking(self):
        # 日志态滞后为 idle, 但思维链在近期刷新 (<=60s) → 状态提升为 RUNNING
        fresh = time.strftime("%H:%M:%S")
        card = self._virtual_card_with_think("idle", fresh)
        self.assertEqual(card["cls"], {"group": "running", "label": "RUNNING"})

    def test_idle_stays_idle_with_stale_thinking(self):
        # 思维链很久没刷新 (>60s) → 保留 IDLE
        stale = time.strftime("%H:%M:%S", time.localtime(time.time() - 7200))
        card = self._virtual_card_with_think("idle", stale)
        self.assertEqual(card["cls"], {"group": "idle", "label": "IDLE"})

    def test_idle_stays_idle_without_thinking(self):
        # 无思维链 → IDLE 保持
        card = self._virtual_card("idle")
        self.assertEqual(card["cls"], {"group": "idle", "label": "IDLE"})


class BoardScannerSourceTest(unittest.TestCase):
    def test_manual_card_is_not_reported_as_discovered(self):
        with tempfile.TemporaryDirectory() as root:
            scanner = SERVER.BoardScanner(root, "20260825")
            base = os.path.join(scanner.board_dir, "manual-agent")
            with open(base + ".pid", "w", encoding="utf-8") as fh:
                fh.write("0")
            with open(base + ".start", "w", encoding="utf-8") as fh:
                fh.write(str(int(time.time())))
            card = scanner.read_agent("manual-agent")
            self.assertEqual(card["source"], "manual")

    def test_discovered_card_keeps_discovered_source(self):
        with tempfile.TemporaryDirectory() as root:
            scanner = SERVER.BoardScanner(root, "20260825")
            discovered = os.path.join(scanner.board_dir, "__discovered__")
            os.makedirs(discovered)
            base = os.path.join(discovered, "auto-agent")
            with open(base + ".pid", "w", encoding="utf-8") as fh:
                fh.write("0")
            with open(base + ".start", "w", encoding="utf-8") as fh:
                fh.write(str(int(time.time())))
            card = scanner.read_agent("auto-agent")
            self.assertEqual(card["source"], "discovered")


class BoardStoreConfigTest(unittest.TestCase):
    def test_interval_must_be_positive(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                SERVER.BoardStore(root, "20260825", 0)


if __name__ == "__main__":
    unittest.main()
