from datetime import datetime, timedelta
from logger import log_event, send_notification

fermentation_state = {}  # {"tilt_color": {"last_gravity": val, "last_change": datetime, "stable_since": datetime or None, "notified": False, "fermentation_started": False, "completion_notified": False}}

def monitor_fermentation(live_tilts):
    now = datetime.utcnow()
    for color, tilt in live_tilts.items():
        gravity = tilt.get("gravity")
        state = fermentation_state.get(color, {})
        last_gravity = state.get("last_gravity")
        last_change = state.get("last_change", now)
        stable_since = state.get("stable_since", None)
        notified = state.get("notified", False)
        fermentation_started = state.get("fermentation_started", False)
        completion_notified = state.get("completion_notified", False)

        if gravity != last_gravity:
            # Gravity changed, reset timers
            fermentation_state[color] = {
                "last_gravity": gravity,
                "last_change": now,
                "stable_since": None,
                "notified": False,
                "fermentation_started": fermentation_started,
                "completion_notified": False
            }
        else:
            # Gravity unchanged
            if not stable_since:
                # Start stability timer
                fermentation_state[color]["stable_since"] = last_change
                fermentation_state[color]["notified"] = False
                fermentation_state[color]["completion_notified"] = False
            else:
                # Check for fermentation completion (24 hours stable after fermentation started)
                hours_stable = (now - stable_since).total_seconds() / 3600.0
                if fermentation_started and hours_stable >= 24 and not completion_notified:
                    # Fermentation completion detected (24 hours without gravity change)
                    msg = (
                        f"Fermentation Completion Detected: Tilt {color}, "
                        f"Time: {now.strftime('%Y-%m-%d %H:%M:%S')}, "
                        f"Gravity stable for 24 hours: {gravity}"
                    )
                    log_event("fermentation_completion", msg, tilt_color=color)
                    fermentation_state[color]["completion_notified"] = True
                
                # Check for final fermentation finished (48 hours stable)
                if hours_stable >= 48 and not notified:
                    # Fermentation finished
                    msg = (
                        f"Fermentation Finished: Tilt {color}, "
                        f"Time: {now.strftime('%Y-%m-%d %H:%M:%S')}, "
                        f"Gravity: {gravity}"
                    )
                    log_event("fermentation_finished", msg, tilt_color=color)
                    fermentation_state[color]["notified"] = True

def mark_fermentation_started(tilt_color):
    """Mark that fermentation has started for a given tilt."""
    if tilt_color in fermentation_state:
        fermentation_state[tilt_color]["fermentation_started"] = True
    else:
        fermentation_state[tilt_color] = {
            "last_gravity": None,
            "last_change": datetime.utcnow(),
            "stable_since": None,
            "notified": False,
            "fermentation_started": True,
            "completion_notified": False
        }
