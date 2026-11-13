"""Runtime configuration, read from environment variables.

On the Pi these come from /etc/signage/signage.env (loaded by systemd).
"""
import os
import secrets
from pathlib import Path


def _env(name, default):
    value = os.environ.get(name)
    return value if value not in (None, "") else default


class Config:
    def __init__(self, data_dir=None):
        self.data_dir = Path(data_dir or _env("SIGNAGE_DATA_DIR", "/var/lib/signage"))
        self.media_dir = self.data_dir / "media"
        self.thumb_dir = self.data_dir / "thumbs"
        self.tmp_dir = self.data_dir / "tmp"
        self.run_dir = self.data_dir / "run"
        self.db_path = self.data_dir / "signage.db"
        self.mpv_socket = self.run_dir / "mpv.sock"

        self.host = _env("SIGNAGE_HOST", "0.0.0.0")
        self.port = int(_env("SIGNAGE_PORT", "80"))
        # Largest single upload, in megabytes. 4K videos get big.
        self.max_upload_mb = int(_env("SIGNAGE_MAX_UPLOAD_MB", "8192"))
        self.session_hours = int(_env("SIGNAGE_SESSION_HOURS", "12"))

        # Player / mpv
        self.mpv_binary = _env("SIGNAGE_MPV", "mpv")
        self.hwdec = _env("SIGNAGE_HWDEC", "drm")
        self.vo_args = _env("SIGNAGE_MPV_VO", "--vo=gpu --gpu-context=drm").split()
        self.audio_device = _env("SIGNAGE_AUDIO_DEVICE", "alsa/hdmi:CARD=vc4hdmi0,DEV=0")
        self.extra_mpv_args = _env("SIGNAGE_MPV_EXTRA", "").split()

        self._secret_key = _env("SIGNAGE_SECRET_KEY", None)

    def ensure_dirs(self):
        for d in (self.data_dir, self.media_dir, self.thumb_dir, self.tmp_dir, self.run_dir):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def secret_key(self):
        """Use SIGNAGE_SECRET_KEY if set, else a key persisted in the data dir."""
        if self._secret_key:
            return self._secret_key
        key_file = self.data_dir / "secret_key"
        if not key_file.exists():
            self.ensure_dirs()
            key_file.write_text(secrets.token_hex(32))
            key_file.chmod(0o600)
        self._secret_key = key_file.read_text().strip()
        return self._secret_key
