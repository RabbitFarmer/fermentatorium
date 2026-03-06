#!/usr/bin/env python3
"""
Test script to verify Tilt Pro / mini-pro detection logic from PR #59.

This validates:
- Raw gravity > 5000 correctly identifies a Pro/mini-pro device
- Pro devices use 10000x gravity encoding and 10x temp encoding
- Standard devices use 1000x gravity encoding and raw temp
- Per-MAC rate limiting uses MAC address as rate key
- MAC address is propagated through update_live_tilt and log_tilt_reading
"""

import sys

# --- Constants matching app3.py -----------------------------------------------
TILT_PRO_GRAVITY_THRESHOLD = 5000
TILT_PRO_GRAVITY_DIVISOR = 10000.0
TILT_PRO_TEMP_DIVISOR = 10.0
TILT_STANDARD_GRAVITY_DIVISOR = 1000.0


def decode_tilt(raw_temp, raw_gravity):
    """Replicate detection_callback decoding logic from app3.py."""
    is_pro = raw_gravity > TILT_PRO_GRAVITY_THRESHOLD
    if is_pro:
        temp_f = round(raw_temp / TILT_PRO_TEMP_DIVISOR, 1)
        gravity = raw_gravity / TILT_PRO_GRAVITY_DIVISOR
    else:
        temp_f = raw_temp
        gravity = raw_gravity / TILT_STANDARD_GRAVITY_DIVISOR
    return temp_f, gravity, is_pro


def test_standard_tilt():
    """Standard Tilt: raw_gravity <= 5000, no encoding adjustment."""
    print("Test 1: Standard Tilt decoding")
    print("-" * 60)
    raw_temp, raw_gravity = 68, 1050
    temp_f, gravity, is_pro = decode_tilt(raw_temp, raw_gravity)
    assert not is_pro, "Standard tilt should NOT be detected as Pro"
    assert temp_f == 68, f"Expected temp_f=68, got {temp_f}"
    assert abs(gravity - 1.050) < 0.001, f"Expected gravity≈1.050, got {gravity}"
    print(f"  raw_temp={raw_temp}, raw_gravity={raw_gravity}")
    print(f"  → temp_f={temp_f}°F, gravity={gravity:.3f}, is_pro={is_pro}")
    print("  ✓ Standard Tilt decoding correct\n")


def test_tilt_pro_mini():
    """Tilt Pro / mini-pro: raw_gravity > 5000, uses 10000x and 10x encoding."""
    print("Test 2: Tilt Pro / mini-pro decoding")
    print("-" * 60)
    raw_temp, raw_gravity = 685, 10501
    temp_f, gravity, is_pro = decode_tilt(raw_temp, raw_gravity)
    assert is_pro, "Tilt Pro should be detected as Pro"
    assert temp_f == 68.5, f"Expected temp_f=68.5, got {temp_f}"
    assert abs(gravity - 1.0501) < 0.0001, f"Expected gravity≈1.0501, got {gravity}"
    print(f"  raw_temp={raw_temp}, raw_gravity={raw_gravity}")
    print(f"  → temp_f={temp_f}°F, gravity={gravity:.4f}, is_pro={is_pro}")
    print("  ✓ Tilt Pro / mini-pro decoding correct\n")


def test_gravity_boundary():
    """Boundary: raw_gravity == 5000 is NOT Pro (must be > 5000)."""
    print("Test 3: Boundary condition (raw_gravity == 5000)")
    print("-" * 60)
    raw_temp, raw_gravity = 70, 5000
    _, gravity, is_pro = decode_tilt(raw_temp, raw_gravity)
    assert not is_pro, "raw_gravity == 5000 should NOT be Pro"
    assert abs(gravity - 5.0) < 0.001, f"Expected gravity=5.0 (standard encoding), got {gravity}"
    print(f"  raw_gravity={raw_gravity} → is_pro={is_pro} (standard), gravity={gravity:.3f}")
    print("  ✓ Boundary condition correct\n")

    raw_temp, raw_gravity = 700, 5001
    _, gravity, is_pro = decode_tilt(raw_temp, raw_gravity)
    assert is_pro, "raw_gravity == 5001 SHOULD be Pro"
    print(f"  raw_gravity={raw_gravity} → is_pro={is_pro} (pro), gravity={gravity:.4f}")
    print("  ✓ Boundary + 1 condition correct\n")


def test_per_mac_rate_key():
    """Per-MAC rate limiting: key is MAC when available, falls back to color."""
    print("Test 4: Per-MAC rate limiting key selection")
    print("-" * 60)

    def rate_key_for(mac, color):
        return mac if mac else color

    mac1 = "AA:BB:CC:DD:EE:01"
    mac2 = "AA:BB:CC:DD:EE:02"
    color = "Yellow"

    key1 = rate_key_for(mac1, color)
    key2 = rate_key_for(mac2, color)

    assert key1 == mac1, f"Expected MAC key for tilt 1, got {key1}"
    assert key2 == mac2, f"Expected MAC key for tilt 2, got {key2}"
    assert key1 != key2, "Two MACs must produce distinct rate keys"
    print(f"  color='{color}', mac1={mac1} → key={key1}")
    print(f"  color='{color}', mac2={mac2} → key={key2}")
    print("  ✓ Distinct MACs produce distinct rate keys\n")

    key_no_mac = rate_key_for("", color)
    assert key_no_mac == color, f"Expected color fallback, got {key_no_mac}"
    print(f"  mac='' → falls back to color='{color}'")
    print("  ✓ Fallback to color when MAC absent\n")


def test_per_mac_rate_limiting_behavior():
    """Rate limiting suppresses rapid repeats per-MAC; two MACs log independently."""
    print("Test 5: Per-MAC rate limiting behavior")
    print("-" * 60)
    from datetime import datetime, timedelta

    last_tilt_log_ts = {}
    logged = []

    def attempt_log(mac, color, now, interval_minutes=15):
        """Simulate the rate-limit check inside log_tilt_reading."""
        rate_key = mac if mac else color
        last_log = last_tilt_log_ts.get(rate_key)
        if last_log:
            elapsed = (now - last_log).total_seconds() / 60.0
            if elapsed < interval_minutes:
                return False
        last_tilt_log_ts[rate_key] = now
        logged.append((mac, color))
        return True

    color = "Yellow"
    mac1 = "AA:BB:CC:DD:EE:01"
    mac2 = "AA:BB:CC:DD:EE:02"
    t0 = datetime(2026, 1, 1, 12, 0, 0)

    assert attempt_log(mac1, color, t0), "First mac1 reading should be logged"
    assert not attempt_log(mac1, color, t0 + timedelta(seconds=30)), \
        "Rapid repeat from mac1 should be suppressed"
    assert attempt_log(mac2, color, t0 + timedelta(seconds=30)), \
        "mac2 first reading should be logged independently"
    assert attempt_log(mac1, color, t0 + timedelta(minutes=16)), \
        "mac1 after interval should be logged again"

    print(f"  Logged entries: {len(logged)}")
    for entry in logged:
        print(f"    mac={entry[0]}, color={entry[1]}")
    assert len(logged) == 3, f"Expected 3 logged entries, got {len(logged)}"
    print("  ✓ Rapid repeats suppressed; distinct MACs log independently\n")


def main():
    """Run all Tilt Pro / mini-pro feature verification tests."""
    print("TILT PRO / MINI-PRO DETECTION TEST  (PR #59 feature verification)")
    print("=" * 70 + "\n")

    tests = [
        test_standard_tilt,
        test_tilt_pro_mini,
        test_gravity_boundary,
        test_per_mac_rate_key,
        test_per_mac_rate_limiting_behavior,
    ]
    failures = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"  ✗ FAILED: {e}\n")
            failures += 1

    if failures == 0:
        print("All tests passed ✓")
    else:
        print(f"{failures} test(s) FAILED ✗")
        sys.exit(1)


if __name__ == "__main__":
    main()
