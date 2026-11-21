#!/bin/zsh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# The first Python 3 that has this app's packages (tkinterdnd2, openpyxl).
# Set AUDIO_POST_TOOLS_PYTHON to choose one yourself.
PYTHON=""
for candidate in "$AUDIO_POST_TOOLS_PYTHON" "$HOME/.pyenv/shims/python3" python3.13 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    [ -n "$candidate" ] || continue
    if "$candidate" -c "import tkinter, tkinterdnd2, openpyxl" >/dev/null 2>&1; then
        PYTHON="$candidate"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "No Python 3 with tkinterdnd2 and openpyxl found."
    echo "Install them with:  pip3 install -r \"$SCRIPT_DIR/requirements.txt\""
    exit 1
fi

export TK_SILENCE_DEPRECATION=1

exec "$PYTHON" "$SCRIPT_DIR/wave_repair.py"
