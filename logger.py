import logging
import os
import json
from datetime import datetime

# --- Legacy Kasa error logging ---
logging.basicConfig(
    filename='logs/kasa_errors.log',
    level=logging.ERROR,
    format='%(asctime)s %(levelname)s %(message)s'
)

def log_error(msg):
    """
    Log Kasa-specific errors to kasa_errors.log and print to terminal.
    Use this for legacy Kasa plug error logging.
    """
    print(msg)  # Terminal output
    logging.error(msg)  # Log to kasa_errors.log

def log_kasa_command(mode, url, action, success=None, error=None):
    """
    Log Kasa plug commands to kasa_activity_monitoring.jsonl.
    By default only errors are logged (success=False). When the
    'enable_kasa_activity_log' system setting is True, all commands
    (including successful ones) are also logged.
    
    Args:
        mode (str): Mode of operation. Expected values: 'heating' or 'cooling'
        url (str): IP address or hostname of the plug (e.g., '192.168.1.100')
        action (str): Action being performed. Expected values: 'on' or 'off'
        success (bool|None): Command success status:
            - None: Command sent, response not yet received
            - True: Command succeeded
            - False: Command failed (always logged)
        error (str|None): Error message if command failed. Only set when success=False
    """
    is_error = (success is False)
    if not is_error:
        try:
            from app3 import system_cfg as _sys_cfg
            if not _sys_cfg.get('enable_kasa_activity_log', False):
                return
        except ImportError:
            return

    try:
        ensure_log_dir()
        log_file = os.path.join(LOG_DIR, 'kasa_activity_monitoring.jsonl')
        
        entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "local_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "mode": mode,
            "url": url,
            "action": action,
        }
        
        if success is not None:
            entry["success"] = success
        
        if error:
            entry["error"] = error
        
        with open(log_file, 'a') as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"[LOG] Failed to log to kasa_activity_monitoring.jsonl: {e}")

# --- General event logging and notifications ---
LOG_DIR = "logs"
BATCHES_DIR = "batches"
TEMP_CONTROL_LOG = 'temp_control/temp_control_log.jsonl'

# Temperature control event types (go to temp_control_log.jsonl)
TEMP_CONTROL_EVENTS = {
    'temp_below_low_limit',
    'temp_above_high_limit',
    'heating_on',
    'heating_off',
    'cooling_on',
    'cooling_off',
    'temp_control_started',
    'temp_control_stopped',
    'temp_control_mode_changed',
}

# Batch event types (go to batch-specific JSONL files)
BATCH_EVENTS = {
    'loss_of_signal',
    'fermentation_starting',
    'fermentation_completion',
    'fermentation_finished',
    'daily_report',
}

def ensure_log_dir():
    """Ensure the /logs directory exists."""
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR)

def ensure_batches_dir():
    """Ensure the /batches directory exists."""
    if not os.path.exists(BATCHES_DIR):
        os.makedirs(BATCHES_DIR)

def log_event(event_type, message, tilt_color=None):
    """
    Log any event to the appropriate log file:
    - Temperature control events go to temp_control_log.jsonl
    - Batch events go to batches/{brewid}.jsonl
    - Other events go to /logs/{event_type}.log
    
    Also triggers notifications (email/PUSH/both) as per system config.
    """
    if event_type in TEMP_CONTROL_EVENTS:
        log_to_temp_control_log(event_type, message, tilt_color)
    elif event_type in BATCH_EVENTS and tilt_color:
        log_to_batch_log(event_type, message, tilt_color)
    else:
        log_to_generic_log(event_type, message, tilt_color)
    
    send_notification(event_type, message, tilt_color)

def log_to_temp_control_log(event_type, message, tilt_color=None):
    """Log temperature control events to temp_control_log.jsonl"""
    try:
        d = os.path.dirname(TEMP_CONTROL_LOG)
        if d and not os.path.exists(d):
            os.makedirs(d, exist_ok=True)
        
        entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "local_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "event_type": event_type,
            "message": message,
        }
        if tilt_color:
            entry["tilt_color"] = tilt_color
        
        with open(TEMP_CONTROL_LOG, 'a') as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"[LOG] Failed to log to temp_control_log.jsonl: {e}")

def log_to_batch_log(event_type, message, tilt_color):
    """Log batch events to batch-specific JSONL file"""
    try:
        ensure_batches_dir()
        
        brewid = tilt_color  # Default to tilt_color
        try:
            from app import tilt_cfg
            brewid = tilt_cfg.get(tilt_color, {}).get("brewid", tilt_color)
        except (ImportError, AttributeError):
            pass
        
        batch_file = f"{BATCHES_DIR}/{brewid}.jsonl"
        
        entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "local_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "event_type": event_type,
            "message": message,
            "tilt_color": tilt_color,
        }
        
        with open(batch_file, 'a') as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"[LOG] Failed to log to batch log: {e}")
        log_to_generic_log(event_type, message, tilt_color)

def log_to_generic_log(event_type, message, tilt_color=None):
    """Log to generic /logs/{event_type}.log file"""
    ensure_log_dir()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    filename = f"{LOG_DIR}/{event_type}.log"
    entry = f"[{timestamp}]"
    if tilt_color:
        entry += f" [{tilt_color}]"
    entry += f" {message}\n"
    with open(filename, "a") as f:
        f.write(entry)

def log_notification(notification_type, subject, body, success, tilt_color=None, error=None):
    """
    Log all notification attempts to notifications_log.jsonl.
    """
    try:
        ensure_log_dir()
        log_file = os.path.join(LOG_DIR, 'notifications_log.jsonl')
        
        entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "local_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "notification_type": notification_type,
            "subject": subject,
            "body": body,
            "success": success,
        }
        
        if tilt_color:
            entry["tilt_color"] = tilt_color
        
        if error:
            entry["error"] = error
        
        with open(log_file, 'a') as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"[LOG] Failed to log to notifications_log.jsonl: {e}")

def send_notification(event_type, message, tilt_color=None):
    """
    Send notifications (email/PUSH/both) according to system_cfg["warning_mode"].
    Only sends if the specific notification type is enabled in system_cfg.
    """
    try:
        from app3 import system_cfg, attempt_send_notifications
    except ImportError:
        return

    enabled = False
    
    if event_type in TEMP_CONTROL_EVENTS:
        temp_notif = system_cfg.get('temp_control_notifications', {})
        notif_key = f'enable_{event_type}'
        enabled = temp_notif.get(notif_key, True)
    
    elif event_type in BATCH_EVENTS:
        batch_notif = system_cfg.get('batch_notifications', {})
        notif_key = f'enable_{event_type}'
        enabled = batch_notif.get(notif_key, True)
    
    else:
        enabled = True
    
    if not enabled:
        return
    
    mode = system_cfg.get("warning_mode", "none").upper()
    if mode in ("EMAIL", "PUSH", "BOTH"):
        subject = f"{event_type.replace('_', ' ').title()} Notification"
        body = message
        if tilt_color:
            body = f"Tilt: {tilt_color}\n{body}"
        attempt_send_notifications(subject, body)
