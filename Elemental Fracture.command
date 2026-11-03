#!/bin/bash
# Double-click entry: starts the helper's interactive menu in Terminal.
# Args are forwarded, e.g. dragging onto Terminal with '--status'.
HERE="$(cd "$(dirname "$0")" && pwd)"

# Prefer the setup-created local venv, then explicit known interpreters.
PY=""
for cand in "$HERE/.venv/bin/python" /usr/local/bin/python3 /opt/homebrew/bin/python3 /usr/bin/python3; do
  if [ -x "$cand" ] && "$cand" -c 'import websocket' 2>/dev/null; then
    PY="$cand"; break
  fi
done

if [ -z "$PY" ]; then
  echo "No python3 with the 'websocket-client' module was found." >&2
  echo "Run Setup.command first (it can create a local .venv)," >&2
  echo "or install it: /usr/local/bin/python3 -m pip install --user websocket-client" >&2
  read -r -p "Press return to close"
  exit 1
fi

"$PY" "$HERE/elemental_fracture.py" "$@"
RC=$?
if [ "$RC" -ne 0 ]; then
  echo "helper exited with status $RC" >&2
  read -r -p "Press return to close"
  exit "$RC"
fi
