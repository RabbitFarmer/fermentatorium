from __future__ import annotations

import random
import time
from dataclasses import dataclass

TILT_COLORS = ["Black", "Blue", "Green", "Orange", "Pink", "Purple", "Red", "Yellow"]

def utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

@dataclass
class SimTilt:
    color: str
    mac: str
    model: int
    gravity: float = 1.050
    temp_f: float = 66.0

    def step(self) -> dict:
        self.gravity = max(0.990, self.gravity + random.uniform(-0.0015, 0.0005))
        self.temp_f = self.temp_f + random.uniform(-0.15, 0.15)
        return {
            "timestamp": utc_iso(),
            "tilt_color": self.color,
            "mac": self.mac,
            "model": self.model,
            "gravity": round(self.gravity, 3),
            "temp_f": round(self.temp_f, 1),
            "rssi": random.randint(-90, -55),
            "source": "sim",
        }

def build_sim_fleet(n_per_color: int = 1) -> list[SimTilt]:
    fleet: list[SimTilt] = []
    for c in TILT_COLORS:
        for i in range(n_per_color):
            model = i % 2
            mac = f"AA:BB:{i:02X}:{ord(c[0]):02X}:{random.randint(0,255):02X}:{random.randint(0,255):02X}"
            fleet.append(SimTilt(color=c, mac=mac, model=model))
    return fleet

def scan_simulated(fleet: list[SimTilt]) -> list[dict]:
    return [t.step() for t in fleet]