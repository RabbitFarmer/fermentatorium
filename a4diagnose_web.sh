#!/usr/bin/env bash
# a4diagnose_web.sh — Diagnose "wrong HTML being served" on the Pi.
#
# Run this in a terminal (bash console) when the browser is displaying old
# or unexpected HTML even after purging the browser cache:
#
#   chmod +x ~/fermentatorium/a4diagnose_web.sh
#   ./a4diagnose_web.sh
#
# The script checks every known source of stale content:
#   1. Which processes are listening on ports 5000 and 5001
#   2. Duplicate / stale Python processes
#   3. Active systemd services (fermentatorium, threecontrol, etc.)
#   4. Whether nginx or apache are running
#   5. Old static index.html files that Flask would serve before routing
#   6. Stale Python __pycache__ bytecode
#   7. Live curl of the server — response headers & first 15 lines of HTML
#   8. /server_info JSON endpoint (confirms which file and PID is serving)
# --------------------------------------------------------------------------

set -uo pipefail

FLASK_PORT="${FLASK_PORT:-5001}"

# ── Colour helpers ──────────────────────────────────────────────────────────
RED='\033[0;31m'; YEL='\033[0;33m'; GRN='\033[0;32m'
CYN='\033[0;36m'; BLD='\033[1m'; RST='\033[0m'
ok()   { echo -e "${GRN}  ✔  $*${RST}"; }
warn() { echo -e "${YEL}  ⚠  $*${RST}"; }
fail() { echo -e "${RED}  ✘  $*${RST}"; }
info() { echo -e "${CYN}     $*${RST}"; }
hdr()  { echo -e "\n${BLD}── $* ──${RST}"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo ""
echo -e "${BLD}╔══════════════════════════════════════════════════════════╗${RST}"
echo -e "${BLD}║         Fermentatorium Web Diagnostic Tool               ║${RST}"
echo -e "${BLD}╚══════════════════════════════════════════════════════════╝${RST}"
echo ""
echo "  App directory : $SCRIPT_DIR"
echo "  Flask port    : $FLASK_PORT"
echo ""

# ── 1. Ports 5000 and 5001 ───────────────────────────────────────────────────
hdr "1. Processes listening on ports 5000 and 5001"

for port in 5000 "$FLASK_PORT"; do
  echo ""
  echo "  Port $port:"
  if command -v ss &>/dev/null; then
    result=$(ss -tlnp 2>/dev/null | awk -v p=":${port}" '$4 ~ p || $4 == "0.0.0.0:"p || $4 == "*:"p' || true)
  elif command -v netstat &>/dev/null; then
    result=$(netstat -tlnp 2>/dev/null | grep ":${port} " || true)
  else
    result=""
  fi

  if [ -z "$result" ]; then
    warn "Nothing listening on port $port."
  else
    echo "$result" | sed 's/^/    /'
    if echo "$result" | grep -q "python\|flask\|gunicorn\|uwsgi\|nginx\|apache"; then
      ok "Found a web server on port $port — check PID/process above."
    else
      info "Something is on port $port but process name not visible — run with sudo for full info."
    fi
  fi

  # lsof gives richer per-process detail
  if command -v lsof &>/dev/null; then
    lof=$(lsof -i ":${port}" -sTCP:LISTEN 2>/dev/null || true)
    if [ -n "$lof" ]; then
      echo ""
      echo "  lsof detail:"
      echo "$lof" | sed 's/^/    /'
    fi
  fi
done

# ── 2. Python processes that look like Flask / old app ───────────────────────
hdr "2. Running Python processes (look for app.py, a4app.py, flask)"
echo ""
py_procs=$(ps aux 2>/dev/null | grep -E 'python.*app\.py|flask' | grep -v grep || true)
if [ -z "$py_procs" ]; then
  ok "No obvious Flask / app.py processes found."
else
  echo "$py_procs" | sed 's/^/  /'
  # Warn if we see old 'app.py' without the a4 prefix
  if echo "$py_procs" | grep -qE '[^a-zA-Z0-9]app\.py'; then
    fail "OLD app.py process detected — this may be serving the stale HTML!"
    info "Kill it with:  kill \$(ps aux | grep 'app.py' | grep -v grep | awk '{print \$2}')"
  fi
  if echo "$py_procs" | grep -q "a4app.py"; then
    ok "a4app.py is running — this is the correct process."
  fi
fi

# ── 3. Systemd services ──────────────────────────────────────────────────────
hdr "3. Systemd services (fermentatorium, threecontrol, old names)"
echo ""
if command -v systemctl &>/dev/null; then
  for svc in fermentatorium.service threecontrol.service ferment.service app.service; do
    state=$(systemctl is-active "$svc" 2>/dev/null || true)
    case "$state" in
      active)
        if [ "$svc" = "fermentatorium.service" ]; then
          ok "$svc is ACTIVE (expected)."
        else
          fail "$svc is ACTIVE — this old service may be running stale code!"
          info "Stop it: sudo systemctl stop $svc && sudo systemctl disable $svc"
        fi
        ;;
      inactive|failed|unknown)
        ok "$svc is $state (not running)."
        ;;
      *)
        warn "$svc: $state"
        ;;
    esac
  done

  echo ""
  echo "  All active services containing 'ferment', 'tilt', or 'control':"
  systemctl list-units --type=service --state=active 2>/dev/null \
    | grep -iE 'ferment|tilt|control|threecontrol|app' \
    | sed 's/^/    /' || info "(none found)"
else
  info "systemctl not available — not a systemd system."
fi

# ── 4. nginx / Apache ────────────────────────────────────────────────────────
hdr "4. nginx / Apache (reverse proxies that could serve cached content)"
echo ""
for webserver in nginx apache2 apache httpd lighttpd; do
  if command -v "$webserver" &>/dev/null || systemctl is-active "${webserver}.service" &>/dev/null 2>&1; then
    state=$(systemctl is-active "${webserver}.service" 2>/dev/null || echo 'unknown')
    if [ "$state" = "active" ]; then
      fail "$webserver is RUNNING — it may be proxying or serving old static files!"
      info "Check its config: sudo ${webserver} -T 2>/dev/null || cat /etc/${webserver}/*.conf"
    else
      ok "$webserver is installed but not active ($state)."
    fi
  fi
done

if ! command -v nginx &>/dev/null && ! command -v apache2 &>/dev/null; then
  ok "nginx and apache2 are not installed."
fi

# ── 5. Stale static index.html files ─────────────────────────────────────────
hdr "5. Stale static index.html (Flask serves these before running any route)"
echo ""
# Flask serves files from the static folder directly; an index.html there
# would shadow the '/' route entirely.
_found_index=0
for check_dir in \
    "$SCRIPT_DIR/static" \
    "$SCRIPT_DIR" \
    /opt/fermentatorium/static \
    /var/www/html \
    /var/www/fermentatorium \
    /srv/http; do
  if [ -f "$check_dir/index.html" ]; then
    _found_index=1
    fail "index.html found at: $check_dir/index.html"
    info "This file would be served instead of the Flask dashboard!"
    info "Remove it: rm '$check_dir/index.html'"
    head -5 "$check_dir/index.html" | sed 's/^/    /'
  fi
done
if [ "$_found_index" -eq 0 ]; then
  ok "No stale index.html files found in common locations."
fi

# ── 6. Python __pycache__ staleness ──────────────────────────────────────────
hdr "6. Python __pycache__ directories"
echo ""
caches=$(find "$SCRIPT_DIR" -type d -name __pycache__ 2>/dev/null | grep -v ".git" || true)
if [ -z "$caches" ]; then
  ok "No __pycache__ directories found."
else
  echo "$caches" | sed 's/^/  /'
  warn "Stale bytecode could mask edits if Flask is run with -B or in unusual mode."
  info "Clear with:  find '$SCRIPT_DIR' -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true"
fi

# ── 7. Live curl — response headers ──────────────────────────────────────────
hdr "7. Live curl of http://127.0.0.1:${FLASK_PORT}/"
echo ""
if command -v curl &>/dev/null; then
  echo "  --- Response headers ---"
  curl_headers=$(curl -sI --max-time 5 "http://127.0.0.1:${FLASK_PORT}/" 2>&1 || true)
  if [ -z "$curl_headers" ]; then
    fail "No response from http://127.0.0.1:${FLASK_PORT}/ — is the app running?"
  else
    echo "$curl_headers" | sed 's/^/    /'

    # Check for the identifying header stamped by a4app.py
    if echo "$curl_headers" | grep -qi "X-Fermentatorium-Server"; then
      ok "X-Fermentatorium-Server header present — response is from a4app.py."
      echo "$curl_headers" | grep -i "X-Fermentatorium-Server" | sed 's/^/    /'
    else
      fail "X-Fermentatorium-Server header MISSING — response is NOT from a4app.py!"
      info "Something else (nginx, old app.py, etc.) is answering on port $FLASK_PORT."
    fi

    # Check for the Server: header (nginx / apache set distinctive values)
    srv_hdr=$(echo "$curl_headers" | grep -i "^Server:" | head -1 || true)
    if [ -n "$srv_hdr" ]; then
      if echo "$srv_hdr" | grep -qi "nginx\|apache\|lighttpd"; then
        fail "Server header shows a reverse proxy: $srv_hdr"
        info "The proxy may be serving cached content instead of forwarding to Flask."
      else
        info "Server header: $srv_hdr"
      fi
    fi

    echo ""
    echo "  --- First 15 lines of body ---"
    curl -s --max-time 5 "http://127.0.0.1:${FLASK_PORT}/" 2>&1 | head -15 | sed 's/^/    /'
  fi
else
  warn "curl not installed — skipping live HTTP check."
fi

# ── 8. /server_info JSON endpoint ────────────────────────────────────────────
hdr "8. /server_info endpoint (which file and PID is actually serving)"
echo ""
if command -v curl &>/dev/null; then
  info_json=$(curl -s --max-time 5 "http://127.0.0.1:${FLASK_PORT}/server_info" 2>/dev/null || true)
  if [ -z "$info_json" ]; then
    warn "/server_info did not respond — app may not be running or is an old version."
  else
    echo "$info_json" | python3 -m json.tool 2>/dev/null || echo "$info_json"
    echo ""
    srv_file=$(echo "$info_json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('server_file','?'))" 2>/dev/null || true)
    if [ -n "$srv_file" ]; then
      if echo "$srv_file" | grep -q "a4app.py"; then
        ok "Server file is a4app.py: $srv_file"
      else
        fail "Server file is NOT a4app.py: $srv_file — wrong version is running!"
      fi
    fi
  fi
else
  warn "curl not installed — skipping /server_info check."
fi

# ── Browser DevTools commands ─────────────────────────────────────────────────
hdr "Browser console commands (paste into DevTools → Console)"
cat <<'EOF'

  // 1. Check which server is answering (look for X-Fermentatorium-Server header):
  fetch('/').then(r => { r.headers.forEach((v,k) => console.log(k+': '+v)); });

  // 2. Check and clear registered service workers (stale SW can serve old HTML
  //    even after "Clear browsing data"):
  navigator.serviceWorker.getRegistrations().then(regs => {
    console.log('Registered service workers:', regs.length);
    regs.forEach(r => { console.log(' scope:', r.scope); r.unregister(); });
  });

  // 3. List all Cache Storage caches and their contents:
  caches.keys().then(names => {
    console.log('Cache Storage names:', names);
    names.forEach(n => caches.open(n).then(c => c.keys().then(ks =>
      ks.forEach(k => console.log(' cache:', n, k.url)))));
  });

  // 4. Confirm server identity via /server_info:
  fetch('/server_info').then(r=>r.json()).then(d=>console.table(d));

EOF

echo ""
echo -e "${BLD}Diagnostic complete.${RST}"
echo ""
