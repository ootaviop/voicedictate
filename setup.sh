#!/usr/bin/env bash
# setup.sh – install voicedictate and its dependencies
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$HOME/.local/share/voicedictate/venv"
BIN_DIR="$HOME/.local/bin"
SERVICE_DIR="$HOME/.config/systemd/user"

echo "╔══════════════════════════════════╗"
echo "║     voicedictate  setup          ║"
echo "╚══════════════════════════════════╝"
echo

# ── 1. system packages ────────────────────────────────────────────────────────
echo "→ Installing system packages (sudo required) …"
sudo apt-get install -y --no-install-recommends \
    xdotool \
    xclip \
    portaudio19-dev \
    python3-venv \
    python3-dev \
    python3-tk \
    libevdev-dev

# ── 2. input group ────────────────────────────────────────────────────────────
if ! id -nG "$USER" | grep -qw input; then
    echo "→ Adding $USER to 'input' group …"
    sudo usermod -aG input "$USER"
    INPUT_GROUP_ADDED=1
else
    INPUT_GROUP_ADDED=0
fi

# ── 3. python venv ────────────────────────────────────────────────────────────
echo "→ Creating venv at $VENV_DIR …"
mkdir -p "$VENV_DIR"
python3 -m venv "$VENV_DIR"

# ── 4. python dependencies ────────────────────────────────────────────────────
echo "→ Installing Python packages …"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install \
    faster-whisper \
    sounddevice \
    evdev \
    numpy

# ── 5. pre-download whisper model ─────────────────────────────────────────────
MODEL="${VOICEDICTATE_MODEL:-base}"
echo "→ Pre-downloading Whisper model '$MODEL' (first run only, may take a minute) …"
"$VENV_DIR/bin/python" -c "
import sys
from faster_whisper import WhisperModel
WhisperModel(sys.argv[1], device='cpu', compute_type='int8')
print('  Model cached.')
" "$MODEL"

# ── 6. launcher script ────────────────────────────────────────────────────────
echo "→ Installing launcher to $BIN_DIR/voicedictate …"
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/voicedictate" <<EOF
#!/usr/bin/env bash
exec "$VENV_DIR/bin/python" "$SCRIPT_DIR/voicedictate.py" "\$@"
EOF
chmod +x "$BIN_DIR/voicedictate"

# ── 7. systemd user service ───────────────────────────────────────────────────
echo "→ Installing systemd user service …"
mkdir -p "$SERVICE_DIR"
cat > "$SERVICE_DIR/voicedictate.service" <<EOF
[Unit]
Description=Voice Dictation Daemon
After=graphical-session.target sound.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=$BIN_DIR/voicedictate
Restart=on-failure
RestartSec=5
# Pass the X11 display through; systemd user units inherit the session env
# when started via graphical-session.target, but the fallback covers cases
# where the service is started before the display env is populated.
Environment=DISPLAY=:0
Environment=PULSE_RUNTIME_PATH=/run/user/%U/pulse
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=graphical-session.target
EOF

systemctl --user daemon-reload
systemctl --user enable voicedictate.service

# ── done ──────────────────────────────────────────────────────────────────────
echo
echo "╔══════════════════════════════════╗"
echo "║         Setup complete!          ║"
echo "╚══════════════════════════════════╝"
echo
echo "  Start now:    systemctl --user start voicedictate"
echo "  Status:        systemctl --user status voicedictate"
echo "  Live logs:     journalctl --user -u voicedictate -f"
echo
echo "  Hotkey:        Hold RIGHT ALT → speak → release"
echo "  Override key:  VOICEDICTATE_KEY=KEY_CAPSLOCK voicedictate"
echo "  Change model:  VOICEDICTATE_MODEL=small voicedictate"
echo "  Language:      VOICEDICTATE_LANG=pt voicedictate"
echo

if [[ "$INPUT_GROUP_ADDED" == "1" ]]; then
    echo "  ⚠  IMPORTANT: You were added to the 'input' group."
    echo "     Log out and back in before running voicedictate."
    echo
fi
