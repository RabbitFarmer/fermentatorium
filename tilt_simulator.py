"""
Simulated Tilt Hydrometer generator and mock BLE scanner for testing.
Allows you to simulate multiple Tilts of various colors, models, and MAC addresses,
generating test gravity and temperature values for each.

Usage:
    - Used for logic/unit tests and multi-device handling.
    - Produces a list of simulated readings in the same style as a real bluepy scan.
"""

import random
import time

TILT_COLORS = ["black", "blue", "green", "orange", "pink", "purple", "red", "yellow"]

class SimulatedTilt:
    def __init__(self, color, model_code, mac, batch_id, gravity=1.050, temp_f=65.0):
        self.color = color
        self.model_code = model_code  # 1=Pro, 0=Standard
        self.mac = mac
        self.batch_id = batch_id
        self.gravity = gravity
        self.temp_f = temp_f

    def generate_reading(self):
        # For simulation, randomly vary gravity and temp a bit
        self.gravity += random.uniform(-0.001, 0.001)
        self.temp_f += random.uniform(-0.1, 0.1)
        return {
            "color": self.color,
            "model_code": self.model_code,
            "mac": self.mac,
            "batch_id": self.batch_id,
            "gravity": round(self.gravity, 3),
            "temp_f": round(self.temp_f, 2),
            "rssi": random.randint(-85, -65),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        }


def generate_mock_tilts(batch_id, n_per_color=1):
    """
    Create a list of SimulatedTilt devices covering all colors and both models.
    Each device gets a unique mock MAC address and starting values.
    """
    tilts = []
    for color in TILT_COLORS:
        for i in range(n_per_color):
            # Alternate model codes and sample MACs, test both standard/pro
            model_code = i % 2  # 0=Standard, 1=Pro
            mac = f"AA:BB:CC:{ord(color[0]):02X}:{i:02X}:{random.randint(0,255):02X}"
            gravity = 1.050 - (0.002 * i)
            temp_f = 66.0 + random.uniform(-1, 1)
            tilts.append(SimulatedTilt(color, model_code, mac, batch_id, gravity, temp_f))
    return tilts

def get_simulated_scan(tilts):
    """
    Simulates a BLE scan, returning current reading from each mock Tilt.
    """
    return [tilt.generate_reading() for tilt in tilts]

# Example use:
if __name__ == "__main__":
    batch_id = "2026A"
    devices = generate_mock_tilts(batch_id, n_per_color=2)
    scan = get_simulated_scan(devices)
    for reading in scan:
        print(reading)