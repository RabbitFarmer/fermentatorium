import json
from datetime import datetime

def generate_brewid(beer_name, batch_name, ferm_start_date):
    import hashlib
    id_str = f"{beer_name}-{batch_name}-{ferm_start_date}"
    return hashlib.sha256(id_str.encode('utf-8')).hexdigest()[:8]

def save_batch_jsonl(color, batch_data, action):
    """
    Append a batch entry to the batches/batch_history_[color].jsonl file in JSONL format.
    
    NOTE: This function is LEGACY code for JSONL event logging.
    The main app uses JSON array format in batch_history_[color].json files instead.
    Use this function only if implementing event-based batch logging.
    
    batch_data must include all batch fields (beer_name, batch_name, etc).
    action is "new" or "edit".
    brewid is generated for "new", preserved for "edit".
    """
    entry = batch_data.copy()
    entry["action"] = action  # "new" or "edit"
    entry["saved_at"] = datetime.utcnow().isoformat()
    if action == "new":
        entry["brewid"] = generate_brewid(
            entry.get("beer_name", ""),
            entry.get("batch_name", ""),
            entry.get("ferm_start_date", "")
        )
    filename = f'batches/batch_history_{color}.jsonl'
    with open(filename, 'a') as f:
        f.write(json.dumps(entry) + "\n")

def load_batch_history_jsonl(color):
    history = []
    filename = f'batches/batch_history_{color}.jsonl'
    try:
        with open(filename, 'r') as f:
            for line in f:
                if line.strip():
                    history.append(json.loads(line))
    except FileNotFoundError:
        pass
    return history

def get_batches_grouped(history):
    """
    Returns a dict: {brewID: [list of history entries for that brewID]}
    """
    grouped = {}
    for entry in history:
        brewid = entry.get("brewid")
        if brewid:
            grouped.setdefault(brewid, []).append(entry)
    return grouped
