#!/usr/bin/env bash
# Installs (or updates) the TV signage player on Raspberry Pi OS.
#
#   sudo ./install.sh            # interactive
#   sudo ./install.sh --yes      # accept the defaults (console boot, boot tweaks)
#   sudo ./install.sh --4k60     # also enable 4K at 60 Hz on the HDMI port
#
# Safe to run again to update: your media, playlist and login are kept.
set -euo pipefail

APP_DIR=/opt/signage
DATA_DIR=/var/lib/signage
ETC_DIR=/etc/signage
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ASSUME_YES=0
ENABLE_4K60=0
BOOT_TWEAKS=1
for arg in "$@"; do
  case "$arg" in
    -y|--yes) ASSUME_YES=1 ;;
    --4k60) ENABLE_4K60=1 ;;
    --no-boot-tweaks) BOOT_TWEAKS=0 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ask() {  # ask "question" -> 0 for yes
  [[ $ASSUME_YES == 1 ]] && return 0
  read -r -p "$1 [Y/n] " reply </dev/tty
  [[ -z "$reply" || "$reply" =~ ^[Yy] ]]
}

if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo: sudo $0 $*" >&2
  exit 1
fi

if ! grep -qi 'raspberry pi' /proc/device-tree/model 2>/dev/null; then
  echo "Warning: this doesn't look like a Raspberry Pi. Continuing anyway."
fi

say "Installing packages (mpv, ffmpeg, Python)"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  mpv ffmpeg python3 python3-venv python3-flask python3-waitress python3-pil

say "Creating the 'signage' service user"
if ! id signage >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin signage
fi
for grp in video render audio input; do
  getent group "$grp" >/dev/null && usermod -aG "$grp" signage
done

say "Copying the app to $APP_DIR"
mkdir -p "$APP_DIR"
rm -rf "$APP_DIR/signage"
cp -r "$SRC_DIR/signage" "$APP_DIR/"
cp "$SRC_DIR/requirements.txt" "$APP_DIR/"
find "$APP_DIR" -name '__pycache__' -prune -exec rm -rf {} +
if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
  # Uses the Debian packages installed above, so no internet downloads from PyPI.
  python3 -m venv --system-site-packages "$APP_DIR/venv"
fi
chown -R root:root "$APP_DIR"

say "Setting up data and config folders"
mkdir -p "$DATA_DIR"/{media,thumbs,tmp,run}
chown -R signage:signage "$DATA_DIR"
chmod 750 "$DATA_DIR"
mkdir -p "$ETC_DIR"
if [[ ! -f "$ETC_DIR/signage.env" ]]; then
  cp "$SRC_DIR/signage.env.example" "$ETC_DIR/signage.env"
fi
chown root:signage "$ETC_DIR/signage.env"
chmod 640 "$ETC_DIR/signage.env"

cat > /usr/local/bin/signage-passwd <<EOF
#!/usr/bin/env bash
# Set the web admin username and password.
set -euo pipefail
[[ \$EUID -eq 0 ]] || { echo "Run with sudo: sudo signage-passwd" >&2; exit 1; }
set -a; . $ETC_DIR/signage.env; set +a
cd $APP_DIR
exec runuser -u signage -- env SIGNAGE_DATA_DIR="\$SIGNAGE_DATA_DIR" \\
  $APP_DIR/venv/bin/python -m signage.cli set-password "\$@"
EOF
chmod 755 /usr/local/bin/signage-passwd

cat > /usr/local/bin/signage-wifi <<EOF
#!/usr/bin/env bash
# Open the Wi-Fi setup wizard in this terminal (works over SSH too).
set -euo pipefail
[[ \$EUID -eq 0 ]] || { echo "Run with sudo: sudo signage-wifi" >&2; exit 1; }
cd $APP_DIR
exec $APP_DIR/venv/bin/python -m signage.wifi_setup --force "\$@"
EOF
chmod 755 /usr/local/bin/signage-wifi

say "Letting the web admin manage Wi-Fi and power"
if [[ -d /etc/polkit-1/rules.d ]]; then
  install -m 644 "$SRC_DIR"/system/polkit/*.rules /etc/polkit-1/rules.d/
  systemctl try-reload-or-restart polkit.service 2>/dev/null || true
else
  echo "polkit isn't installed, so Wi-Fi changes and Restart/Shut down in Settings won't work."
fi

say "Installing systemd services"
cp "$SRC_DIR/systemd/signage-web.service" "$SRC_DIR/systemd/signage-player.service" \
   "$SRC_DIR/systemd/signage-wifi-setup.service" /etc/systemd/system/
systemctl daemon-reload

# The player draws straight to the HDMI output, which needs the desktop off.
if [[ "$(systemctl get-default)" == "graphical.target" ]]; then
  echo
  echo "This Pi boots to the desktop. The player needs the screen to itself,"
  echo "so the Pi should boot to the console instead (the TV will only show the signage)."
  if ask "Switch to console boot?"; then
    systemctl set-default multi-user.target
    NEED_REBOOT=1
  else
    echo "Left as is. Set SIGNAGE_MPV_VO=--vo=gpu in $ETC_DIR/signage.env and run the player"
    echo "inside the desktop session instead (see README)."
  fi
fi

BOOT_DIR=/boot/firmware
[[ -d $BOOT_DIR ]] || BOOT_DIR=/boot
if [[ $BOOT_TWEAKS == 1 && -f $BOOT_DIR/cmdline.txt ]]; then
  if ! grep -q 'vt.global_cursor_default=0' "$BOOT_DIR/cmdline.txt"; then
    if ask "Hide the console cursor and stop the screen blanking (edits $BOOT_DIR/cmdline.txt)?"; then
      cp "$BOOT_DIR/cmdline.txt" "$BOOT_DIR/cmdline.txt.signage-backup"
      sed -i '1 s/$/ consoleblank=0 vt.global_cursor_default=0 logo.nologo/' "$BOOT_DIR/cmdline.txt"
      NEED_REBOOT=1
    fi
  fi
fi
if [[ $ENABLE_4K60 == 1 && -f $BOOT_DIR/config.txt ]] && ! grep -q '^hdmi_enable_4kp60=1' "$BOOT_DIR/config.txt"; then
  cp "$BOOT_DIR/config.txt" "$BOOT_DIR/config.txt.signage-backup"
  printf '\n[pi4]\n# Added by signage installer: allow 4K at 60 Hz\nhdmi_enable_4kp60=1\n[all]\n' >> "$BOOT_DIR/config.txt"
  NEED_REBOOT=1
fi

# Admin login
if ! (cd "$APP_DIR" && runuser -u signage -- "$APP_DIR/venv/bin/python" -) <<EOF
import sys
from signage import db
from signage.config import Config
conn = db.connect(Config("$DATA_DIR").db_path)
sys.exit(0 if db.get_setting(conn, "admin_password_hash") else 1)
EOF
then
  say "Choose the web admin login"
  if [[ -n "${SIGNAGE_ADMIN_PASSWORD:-}" ]]; then
    SIGNAGE_ADMIN_PASSWORD="$SIGNAGE_ADMIN_PASSWORD" /usr/local/bin/signage-passwd --username "${SIGNAGE_ADMIN_USER:-admin}"
  else
    /usr/local/bin/signage-passwd </dev/tty
  fi
fi

say "Starting services"
systemctl enable signage-web.service signage-player.service signage-wifi-setup.service
systemctl restart signage-web.service
if [[ "$(systemctl get-default)" != "graphical.target" ]] || [[ -n "${NEED_REBOOT:-}" ]]; then
  systemctl restart signage-player.service || true
fi

PORT=$(grep -E '^SIGNAGE_PORT=' "$ETC_DIR/signage.env" | cut -d= -f2)
PORT=${PORT:-80}
SUFFIX=$([[ $PORT == 80 ]] && echo "" || echo ":$PORT")
IP=$(hostname -I | awk '{print $1}')
say "Done"
echo "Open the admin page from a computer or phone on the same network:"
echo "    http://$(hostname).local$SUFFIX    or    http://$IP$SUFFIX"
if [[ -n "${NEED_REBOOT:-}" ]]; then
  echo
  echo "Reboot to finish setting up the screen:  sudo reboot"
fi
