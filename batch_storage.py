import os
import json
import re

def sanitize(s):
    # Remove non-alphanumeric, replace spaces with underscores
    return re.sub(r'[^A-Za-z0-9]+', '_', s).strip('_')

def batch_filename(color, beer_name, batch_name, date_str, brewid):
    # Use only a few characters of brewid to keep name manageable
    color = sanitize(color)
    beer = sanitize(beer_name)
    batch = sanitize(batch_name)
    date = date_str.replace('-', '')
    short_id = brewid[:8]
    return f"Batches/{color}_{beer}_{batch}_{date}_{short_id}.jsonl"

def append_batch_record(color, beer_name, batch_name, date_str, brewid, record):
    os.makedirs("Batches", exist_ok=True)
    fname = batch_filename(color, beer_name, batch_name, date_str, brewid)
    with open(fname, "a") as f:
        f.write(json.dumps(record) + "\n")

def get_batch_history(color, beer_name, batch_name, date_str, brewid):
    fname = batch_filename(color, beer_name, batch_name, date_str, brewid)
    if not os.path.exists(fname):
        return []
    with open(fname) as f:
        return [json.loads(line) for line in f]

def list_batches():
    # List all batch files and show user-friendly info
    os.makedirs("Batches", exist_ok=True)
    files = os.listdir("Batches")
    batches = []
    for fname in files:
        m = re.match(r"([A-Za-z]+)_([A-Za-z0-9_]+)_([A-Za-z0-9_]+)_([0-9]{8})_([a-f0-9]{8})\.jsonl", fname)
        if m:
            batches.append({
                "color": m.group(1),
                "beer": m.group(2),
                "batch": m.group(3),
                "date": m.group(4),
                "brewid_short": m.group(5),
                "filename": fname
            })
    return batches
