"""Hardware-free tests for heating and cooling fault detection."""
import ast
import os
import unittest
from unittest.mock import Mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_fault_functions(namespace):
    with open(os.path.join(ROOT, "app.py"), encoding="utf-8") as source:
        tree = ast.parse(source.read())
    names = {"check_heating_cooling_faults", "_send_fault_notification"}
    nodes = [node for node in tree.body
             if isinstance(node, ast.FunctionDef) and node.name in names]
    exec(compile(ast.Module(nodes, []), "app.py", "exec"), namespace)


class TemperatureFaultTests(unittest.TestCase):
    def setUp(self):
        self.clock = Mock()
        self.clock.monotonic.return_value = 0
        self.manager = Mock()
        self.log = Mock()
        self.notifications = Mock()
        self.live_temp = Mock(return_value=38.0)
        self.sensor_active = Mock(return_value=True)
        self.controller = {
            "controller_id": 0, "temp_control_active": True,
            "cooler_on": True, "cooling_plug": "192.0.2.1",
            "current_temp": 38.0, "tilt_color": "Orange",
            "low_limit": 36.0, "high_limit": 38.0,
        }
        self.namespace = {
            "temp_cfg": {"controllers": [self.controller]},
            "time": self.clock,
            "kasa_manager": self.manager,
            "_FORCE_IOT_PORT": False,
            "_is_valid_controller_id": lambda value: value in (0, 1, 2),
            "append_control_log": self.log,
            "attempt_send_notifications": self.notifications,
            "get_current_temp_for_control_tilt": self.live_temp,
            "is_control_tilt_active": self.sensor_active,
            "get_control_tilt_color": lambda controller: controller["tilt_color"],
            "FAULT_RESPONSE_WINDOW_SECONDS": 1800,
        }
        load_fault_functions(self.namespace)
        self.check = self.namespace["check_heating_cooling_faults"]

    def sample(self, minute, temp):
        self.clock.monotonic.return_value = minute * 60
        self.controller["current_temp"] = temp
        self.check()

    def test_slow_cooling_progress_never_alerts_on_flat_intermediate_readings(self):
        for minute, temp in [(0, 38.0), (2, 38.0), (10, 37.9),
                             (12, 37.9), (24, 37.8), (28, 37.8),
                             (48, 37.7), (60, 37.6)]:
            self.sample(minute, temp)
        self.notifications.assert_not_called()
        self.manager.send.assert_not_called()

    def test_fault_warns_once_per_cycle_and_rearms_after_off(self):
        self.sample(0, 38.0)
        self.sample(29, 38.0)
        self.notifications.assert_not_called()
        self.sample(30, 38.0)
        self.assertTrue(self.controller["cooling_fault_detected"])
        self.assertEqual(self.notifications.call_count, 1)
        self.assertEqual(self.manager.send.call_count, 1)
        self.assertIn("30 minutes", self.notifications.call_args.kwargs["body"])
        self.sample(32, 37.9)
        self.assertFalse(self.controller["cooling_fault_detected"])
        self.sample(62, 37.9)
        self.sample(64, 37.9)
        self.assertEqual(self.notifications.call_count, 1)
        self.assertEqual(self.manager.send.call_count, 1)
        self.controller["cooler_on"] = False
        self.controller["current_temp"] = None
        self.check()
        self.controller["cooler_on"] = True
        self.sample(65, 38.0)
        self.sample(95, 38.0)
        self.assertEqual(self.notifications.call_count, 2)

    def test_heating_wrong_direction_and_independent_controller(self):
        self.controller.update(cooler_on=False, heater_on=True, heating_plug="192.0.2.2")
        other = dict(self.controller, controller_id=1, current_temp=65.0)
        self.namespace["temp_cfg"]["controllers"].append(other)
        self.sample(0, 66.0)
        self.sample(30, 65.9)
        self.assertEqual(self.notifications.call_count, 2)
        self.assertTrue(self.controller["heating_fault_detected"])
        self.assertTrue(other["heating_fault_detected"])
        self.assertEqual(self.manager.send.call_count, 2)

    def test_interruption_and_pending_off_restart_window_without_reset(self):
        self.sample(0, 38.0)
        self.sensor_active.return_value = False
        self.sample(30, 38.0)
        self.sensor_active.return_value = True
        self.sample(31, 38.0)
        self.controller["cooler_pending_action"] = "off"
        self.sample(61, 38.0)
        self.controller["cooler_pending_action"] = None
        self.live_temp.return_value = None
        self.sample(62, 38.0)
        self.live_temp.return_value = 38.0
        self.sample(63, 38.0)
        self.sample(92, 38.0)
        self.notifications.assert_not_called()
        self.sample(93, 38.0)
        self.assertEqual(self.notifications.call_count, 1)

    def test_notification_does_not_claim_unavailable_reset(self):
        self.controller["cooling_plug"] = ""
        self.sample(0, 38.0)
        self.sample(30, 38.0)
        self.manager.send.assert_not_called()
        self.assertIn("No plug reset was requested", self.notifications.call_args.kwargs["body"])

    def test_partial_recovery_from_wrong_direction_does_not_restart_window(self):
        self.sample(0, 38.0)
        self.sample(10, 38.5)
        self.sample(20, 38.2)
        self.sample(30, 38.1)
        self.assertEqual(self.notifications.call_count, 1)

    def test_signal_interruption_does_not_rearm_notification(self):
        self.sample(0, 38.0)
        self.sample(30, 38.0)
        self.sensor_active.return_value = False
        self.sample(31, 38.0)
        self.sensor_active.return_value = True
        self.sample(32, 38.0)
        self.sample(62, 38.0)
        self.assertEqual(self.notifications.call_count, 1)

    def test_new_sensor_starts_a_new_window(self):
        self.sample(0, 38.0)
        self.controller["tilt_color"] = "Blue"
        self.sample(30, 38.0)
        self.notifications.assert_not_called()
        self.sample(60, 38.0)
        self.assertEqual(self.notifications.call_count, 1)


if __name__ == "__main__":
    unittest.main()
