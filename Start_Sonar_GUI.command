#!/bin/bash
# Airborne Sonar GUI launcher for macOS (and Linux).
# First run: creates a private Python environment in .venv and installs the
# requirements into it. Later runs just start the application.
cd "$(dirname "$0")" || exit 1

fail() {
    echo
    echo "$1"
    read -r -p "Press Enter to close this window." _
    exit 1
}

PYTHON_BIN="${PYTHON:-python3}"
command -v "$PYTHON_BIN" >/dev/null 2>&1 ||
    fail "Python 3 was not found. Install Python 3.9 or newer from https://www.python.org/downloads/ and run this again."

if [ ! -x .venv/bin/python ]; then
    echo "First run: creating the Python environment (.venv)..."
    "$PYTHON_BIN" -m venv .venv || { rm -rf .venv; fail "Could not create the environment. Install Python 3 from python.org."; }
fi

if ! .venv/bin/python -c "import sys, tkinter; sys.exit(0 if tkinter.TkVersion >= 8.6 else 1)" 2>/dev/null; then
    rm -rf .venv
    fail "This Python has no usable Tk 8.6 (the macOS built-in Python is too old). Install Python 3 from https://www.python.org/downloads/ and run this again."
fi

if [ ! -f .venv/.deps_ok ]; then
    echo "Installing the requirements (one time, needs internet, a few minutes)..."
    .venv/bin/python -m pip install --upgrade pip &&
        .venv/bin/python -m pip install -r requirements.txt &&
        touch .venv/.deps_ok ||
        fail "Installing the requirements failed. Check the internet connection and run this again."
fi

exec .venv/bin/python host/sonar_gui.py
