import logging
import os
import json
import smtplib
from datetime import datetime
from email.mime.text import MIMEText

# --- Legacy Kasa error logging ---
logging.basicConfig(
    filename='logs/kasa_errors.log',
    level=logging.ERROR,
    format='%(asctime)s %(levelname)s %(message)s'
)

_SYSTEM_CFG_PATH = 'config/system_config.json'

def _load_system_cfg():
    """Read system_config.json from disk. Returns empty dict on any error."""
    try:
        with open(_SYSTEM_CFG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

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
        sys_cfg = _load_system_cfg()
        if not sys_cfg.get('enable_kasa_activity_log', False):
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
    sys_cfg = _load_system_cfg()
    mode = sys_cfg.get("warning_mode", "none").upper()
    if mode not in ("EMAIL", "PUSH", "BOTH"):
        return

    enabled = False
    
    if event_type in TEMP_CONTROL_EVENTS:
        temp_notif = sys_cfg.get('temp_control_notifications', {})
        notif_key = f'enable_{event_type}'
        enabled = temp_notif.get(notif_key, True)
    
    elif event_type in BATCH_EVENTS:
        batch_notif = sys_cfg.get('batch_notifications', {})
        notif_key = f'enable_{event_type}'
        enabled = batch_notif.get(notif_key, True)
    
    else:
        enabled = True
    
    if not enabled:
        return
    
    subject = f"{event_type.replace('_', ' ').title()} Notification"
    body = message
    if tilt_color:
        body = f"Tilt: {tilt_color}\n{body}"
    attempt_send_notifications(subject, body, sys_cfg)


def _smtp_send(recipient, subject, body, cfg):
    """Send an email via SMTP using the provided config dict."""
    sending_email = cfg.get("sending_email") or cfg.get("email")
    if not (isinstance(cfg, dict) and sending_email):
        error_msg = "SMTP configuration incomplete: sender email not configured"
        print(f"[LOG] {error_msg}")
        return False, error_msg
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = sending_email
        msg["To"] = recipient
        server = smtplib.SMTP(cfg.get("smtp_host", "localhost"),
                              int(cfg.get("smtp_port", 25)), timeout=10)
        if cfg.get("smtp_starttls"):
            server.starttls()
        smtp_password = cfg.get("smtp_password") or cfg.get("sending_email_password")
        if sending_email and smtp_password:
            server.login(sending_email, smtp_password)
        server.sendmail(sending_email, [recipient], msg.as_string())
        server.quit()
        return True, "Success"
    except Exception as e:
        original_error = str(e)
        print(f"[LOG] SMTP send failed: {original_error}")
        if ("BadCredentials" in original_error or
                ("535" in original_error and "gmail" in cfg.get("smtp_host", "").lower())):
            error_msg = (
                "Gmail authentication failed. Gmail requires an App Password when "
                "2-Factor Authentication is enabled. To fix: 1) Enable 2FA on your "
                "Google account, 2) Generate an App Password at "
                "https://myaccount.google.com/apppasswords, 3) Use that App Password "
                f"in the Fermenter Email Password field. Original error: {original_error}"
            )
        else:
            error_msg = original_error
        return False, error_msg


def send_email(subject, body, cfg=None):
    """Send an email notification."""
    if cfg is None:
        cfg = _load_system_cfg()
    recipient = cfg.get("email")
    if not recipient:
        print("[LOG] No recipient email configured")
        return False, "No recipient email configured"
    return _smtp_send(recipient, subject, body, cfg)


def _send_push_pushover(body, subject, cfg):
    """Send push notification via Pushover."""
    try:
        import requests as _req
    except ImportError:
        _req = None
    if not _req:
        return False, "requests library not installed"
    user_key = cfg.get("pushover_user_key", "").strip()
    api_token = cfg.get("pushover_api_token", "").strip()
    if not user_key or not api_token:
        return False, "Pushover User Key and API Token must be configured"
    try:
        url = "https://api.pushover.net/1/messages.json"
        payload = {"token": api_token, "user": user_key, "title": subject,
                   "message": body, "priority": 0}
        device = cfg.get("pushover_device", "").strip()
        if device:
            payload["device"] = device
        resp = _req.post(url, data=payload, timeout=10)
        if resp.status_code == 200:
            return True, "Success"
        return False, f"Pushover returned status {resp.status_code}"
    except Exception as e:
        return False, f"Pushover push failed: {e}"


def _send_push_ntfy(body, subject, cfg):
    """Send push notification via ntfy."""
    try:
        import requests as _req
    except ImportError:
        _req = None
    if not _req:
        return False, "requests library not installed"
    ntfy_server = cfg.get("ntfy_server", "https://ntfy.sh").strip()
    ntfy_topic = cfg.get("ntfy_topic", "").strip()
    if not ntfy_topic:
        return False, "ntfy Topic must be configured"
    try:
        url = f"{ntfy_server}/{ntfy_topic}"
        headers = {"Title": subject, "Priority": "default", "Tags": "beer,fermentation"}
        auth_token = cfg.get("ntfy_auth_token", "").strip()
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        resp = _req.post(url, data=body.encode('utf-8'), headers=headers, timeout=10)
        if resp.status_code == 200:
            return True, "Success"
        return False, f"ntfy returned status {resp.status_code}"
    except Exception as e:
        return False, f"ntfy push failed: {e}"


def send_push(body, subject="Fermenter Notification", cfg=None):
    """Send a push notification using the configured provider (Pushover or ntfy)."""
    if cfg is None:
        cfg = _load_system_cfg()
    push_provider = cfg.get("push_provider", "pushover").lower()
    if push_provider == "ntfy":
        return _send_push_ntfy(body, subject, cfg)
    return _send_push_pushover(body, subject, cfg)


def attempt_send_notifications(subject, body, cfg=None):
    """
    Attempt to send email/push notifications according to system_cfg["warning_mode"].
    Returns True if at least one notification succeeded.
    """
    if cfg is None:
        cfg = _load_system_cfg()
    mode = (cfg.get('warning_mode') or 'NONE').upper()
    success_any = False
    error_msg = None

    try:
        if mode == 'EMAIL':
            success_any, error_msg = send_email(subject, body, cfg)
            if not success_any:
                print(f"[LOG] Email notification failed: {error_msg}")
            log_notification('email', subject, body, success_any,
                             error=error_msg if not success_any else None)
        elif mode == 'PUSH':
            success_any, error_msg = send_push(body, subject, cfg)
            if not success_any:
                print(f"[LOG] Push notification failed: {error_msg}")
            log_notification('push', subject, body, success_any,
                             error=error_msg if not success_any else None)
        elif mode == 'BOTH':
            email_ok, email_err = send_email(subject, body, cfg)
            push_ok, push_err = send_push(body, subject, cfg)
            success_any = email_ok or push_ok
            log_notification('email', subject, body, email_ok,
                             error=email_err if not email_ok else None)
            log_notification('push', subject, body, push_ok,
                             error=push_err if not push_ok else None)
    except Exception as e:
        print(f"[LOG] Unexpected error in attempt_send_notifications: {e}")

    return success_any
