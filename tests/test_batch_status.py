"""Hardware-free tests for batch status derived from persisted event logs."""
import ast
import os
import unittest
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_calculate_batch_statistics():
    with open(os.path.join(ROOT, "app.py"), encoding="utf-8") as src:
        tree = ast.parse(src.read())
    node = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef)
                and n.name == "calculate_batch_statistics")
    namespace = {"datetime": datetime}
    exec(compile(ast.Module([node], []), "app.py", "exec"), namespace)
    return namespace["calculate_batch_statistics"]


class BatchStatusTests(unittest.TestCase):
    def setUp(self):
        self.calculate = _load_calculate_batch_statistics()

    def test_logged_start_event_is_used_without_notification_state(self):
        batch_data = [
            {"event": "sample", "payload": {"gravity": 1.050, "timestamp": "2026-11-01T10:00:00Z"}},
            {"event_type": "fermentation_starting", "timestamp": "2026-11-02T10:00:00Z"},
        ]

        stats = self.calculate(batch_data, {})

        self.assertEqual(stats["ferm_status"], "Active Fermentation — started 2026-11-02")

    def test_logged_completion_event_is_used_without_notification_state(self):
        batch_data = [
            {"event": "sample", "payload": {"gravity": 1.010}},
            {"event_type": "fermentation_completion", "timestamp": "2026-11-03T10:00:00Z"},
        ]

        stats = self.calculate(batch_data, {})

        self.assertEqual(stats["ferm_status"], "Fermentation Complete — 2026-11-03")

    def test_samples_without_start_event_remain_pre_fermentation(self):
        batch_data = [{"event": "sample", "payload": {"gravity": 1.050}}]

        stats = self.calculate(batch_data, {})

        self.assertEqual(stats["ferm_status"], "Pre-Fermentation / Conditioning")


if __name__ == "__main__":
    unittest.main()
