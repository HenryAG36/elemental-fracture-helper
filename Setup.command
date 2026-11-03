#!/bin/bash
# Double-click setup entry: check / apply / undo for the helper.
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" || exit 1

pick_python() {
  # Prefer the local venv, then probe explicit known interpreters.
  if [ -x "$HERE/.venv/bin/python" ]; then
    echo "$HERE/.venv/bin/python"; return 0
  fi
  for cand in /usr/local/bin/python3 /opt/homebrew/bin/python3 /usr/bin/python3; do
    if [ -x "$cand" ]; then echo "$cand"; return 0; fi
  done
  return 1
}

PY="$(pick_python)"
if [ -z "$PY" ]; then
  echo "No python3 found on this machine." >&2
  read -r -p "Press return to close"; exit 1
fi

# websocket-client is needed only by the runtime helper, but set up the
# pinned local venv once so both commands work the same way. A .venv
# that exists but can't import websocket counts as broken -> retry.
VENV_OK=0
if [ -x "$HERE/.venv/bin/python" ] && "$HERE/.venv/bin/python" -c 'import websocket' 2>/dev/null; then
  VENV_OK=1
fi
if [ "$VENV_OK" -eq 0 ]; then
  if [ -d "$HERE/.venv" ]; then
    echo "Existing .venv is missing websocket-client — repairing."
  else
    echo "No local .venv yet."
  fi
  read -r -p "Create/update .venv with pinned websocket-client==1.9.0 from PyPI (needs network)? [y/N] " ANS
  case "$ANS" in
    y|Y|yes|YES)
      [ -x "$HERE/.venv/bin/python" ] || "$PY" -m venv "$HERE/.venv" || { echo "venv creation failed" >&2; read -r -p "Press return to close"; exit 1; }
      "$HERE/.venv/bin/pip" install -r "$HERE/requirements.txt" || { echo "pip install failed" >&2; read -r -p "Press return to close"; exit 1; }
      PY="$HERE/.venv/bin/python"
      ;;
    *) echo "Skipping venv setup (apply/check/undo do not need it)." ;;
  esac
fi

echo
read -r -p "Bottle name [Spellbreak]: " BN
BN="${BN:-Spellbreak}"

echo
echo "  1) check   — read-only status (hashes, config, journal)"
echo "  2) apply   — patch launcher + set xinput1_3 override"
echo "  3) undo    — revert what a recorded apply did"
echo "  q) quit"
read -r -p "> " CH

case "$CH" in
  1) "$PY" "$HERE/setup_helper.py" check --bottle "$BN" "$@" ;;
  2) "$PY" "$HERE/setup_helper.py" apply --bottle "$BN" "$@" ;;
  3) "$PY" "$HERE/setup_helper.py" undo --bottle "$BN" "$@" ;;
  *) echo "bye"; exit 0 ;;
esac
RC=$?
if [ "$RC" -ne 0 ]; then
  echo "(exited with status $RC)" >&2
fi
read -r -p "Press return to close"
exit "$RC"
