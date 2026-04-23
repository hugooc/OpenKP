#!/usr/bin/env bash
#
# OpenKP developer setup.
#
# Creates a Python venv under openkp/.venv, installs OpenKP in editable mode
# with dev extras, and installs the Playwright Chromium browser binary.
#
# Idempotent. Safe to run again any time (e.g., after a dependency bump).
#
# Usage:
#   bash scripts/setup-dev.sh
#
# Requirements:
#   - macOS or Linux
#   - Python 3.11 or newer (python3.11, python3.12, etc.)
#

set -euo pipefail

WORKSPACE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG_DIR="$WORKSPACE/openkp"
VENV_DIR="$PKG_DIR/.venv"

echo "OpenKP developer setup"
echo "  Workspace: $WORKSPACE"
echo "  Package:   $PKG_DIR"
echo "  Venv:      $VENV_DIR"
echo

# --- 1. Find a suitable Python interpreter ------------------------------------

pick_python() {
    for candidate in python3.13 python3.12 python3.11 python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            local version
            version="$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
            local major="${version%.*}"
            local minor="${version#*.}"
            if [[ "$major" -ge 3 && "$minor" -ge 11 ]]; then
                echo "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

PYTHON="$(pick_python)" || {
    echo "ERROR: no Python 3.11+ found on PATH." >&2
    echo "Install one via pyenv, Homebrew (brew install python@3.12), or python.org." >&2
    exit 1
}

PY_VERSION="$("$PYTHON" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
echo "Using $PYTHON (Python $PY_VERSION)"

# --- 2. Create venv (idempotent) ----------------------------------------------

if [[ ! -d "$VENV_DIR" ]]; then
    echo "Creating venv at $VENV_DIR"
    "$PYTHON" -m venv "$VENV_DIR"
else
    echo "Venv already exists, reusing"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# --- 3. Install package + dev deps --------------------------------------------

echo "Upgrading pip"
pip install --quiet --upgrade pip

echo "Installing OpenKP in editable mode with [dev] extras"
pip install --quiet -e "$PKG_DIR[dev]"

# --- 4. Playwright Chromium ---------------------------------------------------

echo "Installing Playwright Chromium (this can take a minute on first run)"
playwright install chromium

# --- 5. Smoke tests -----------------------------------------------------------

echo
echo "Smoke tests:"

OPENKP_BIN="$VENV_DIR/bin/openkp"
if [[ ! -x "$OPENKP_BIN" ]]; then
    echo "  FAIL: openkp binary not found at $OPENKP_BIN" >&2
    exit 1
fi
echo "  openkp binary: $OPENKP_BIN"

echo "  Running pytest"
(cd "$PKG_DIR" && python -m pytest -q) || {
    echo "  FAIL: pytest did not pass" >&2
    exit 1
}

# --- 6. Done ------------------------------------------------------------------

cat <<EOF

Setup complete.

Next steps:
  1. Configure credentials:
       cp $PKG_DIR/.env.example $PKG_DIR/.env
       # Edit .env, set KP_USERNAME. Then store the password in the keychain:
       python -c 'import keyring; keyring.set_password("openkp", "YOUR_USERNAME", "YOUR_PASSWORD")'

  2. Wire Claude Desktop.
       Open ~/Library/Application\\ Support/Claude/claude_desktop_config.json
       and paste (merge with existing mcpServers block if present):

       {
         "mcpServers": {
           "openkp": {
             "command": "$OPENKP_BIN"
           }
         }
       }

       Fully quit and relaunch Claude Desktop.

  3. In a new Claude chat, ask:
       "Use openkp to ping and then tell me who I am."
       Expect: "pong" and your configured KP username.

EOF
