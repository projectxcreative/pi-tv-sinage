# Pi TV Signage

Plays a looping playlist of videos and photos full screen on a TV from a
Raspberry Pi 4B, managed from a password-protected web page on your network.

- Upload videos and photos from any browser (drag and drop, with progress)
- Reorder by dragging (or the arrow buttons on a phone), switch items on/off, delete
- Set how long each photo shows (one default, or per photo)
- See what's on screen, skip to the next item, or play a chosen item now
- Mute, and choose "fit with black bars" or "fill the screen"
- Starts on boot and restarts itself if anything crashes

## How it works

| Part | What it does |
|---|---|
| `signage-player` service | Runs [mpv](https://mpv.io) full screen straight to HDMI (no desktop needed), using the Pi's hardware video decoder. It keeps the next item queued so videos and photos follow each other without gaps, and picks up playlist changes within a second or two. |
| `signage-web` service | The admin website (Flask + Waitress) on port 80. Stores files in `/var/lib/signage/media` and the playlist in a small SQLite database. |

## About 4K on the Raspberry Pi 4B

The Pi 4B can only decode 4K smoothly if the video is **H.265 (HEVC)**. Its
H.264 decoder stops at 1080p, so a 4K H.264 file (the usual "MP4" from phones
and editing apps) will stutter. The admin page detects this on upload and
shows a warning on the item.

### Convert for TV (Mac)

`tools/mac/Convert for TV.applescript` is a drag-and-drop app that converts
videos to 4K H.265 with macOS's built-in `avconvert`, using the Mac's hardware
encoder (nothing to install). A 13-minute 4K video takes about 6 minutes on an
Apple Silicon Mac. Build it once:

```bash
osacompile -o ~/Applications/"Convert for TV.app" tools/mac/"Convert for TV.applescript"
```

Drop videos on it (or double-click it and choose them). Each converted copy is
saved next to the original as `<name> (TV 4K HEVC).mp4`; upload that one.

Converting on the Pi itself isn't practical: it has no H.265 encoder, so a
13-minute 4K video takes 4–5 hours in software.

Other ways to convert to 4K HEVC (free, with [ffmpeg](https://ffmpeg.org) or HandBrake):

```bash
ffmpeg -i input.mp4 -c:v libx265 -crf 22 -preset medium -tag:v hvc1 \
       -vf "scale=3840:2160:force_original_aspect_ratio=decrease" \
       -c:a aac -b:a 160k output-4k-hevc.mp4
```

In HandBrake, pick the "H.265 MKV 2160p60 4K" preset or set Video Encoder to H.265.
1080p H.264 files play fine as they are. Photos are automatically rotated and
shrunk to at most 3840×2160 on upload.

By default the Pi outputs 4K at 30 Hz. Run the installer with `--4k60` to allow
4K at 60 Hz (needs a good HDMI cable and a TV that supports it).

## Install

You need a Raspberry Pi 4B with **Raspberry Pi OS Lite (64-bit)**, Bookworm or
newer, connected to your network and the TV. The desktop version works too: the
installer offers to switch it to boot to the console, so the TV shows only the
signage.

1. Flash Raspberry Pi OS Lite (64-bit) with Raspberry Pi Imager. In its settings,
   set a hostname (e.g. `signage`), your Wi-Fi, and enable SSH.
2. On the Pi (over SSH or Raspberry Pi Connect's browser shell), download the
   code and run the installer:

   ```bash
   sudo apt update && sudo apt install -y git
   git clone https://github.com/projectxcreative/pi-tv-sinage.git ~/pi-tv-sinage
   cd ~/pi-tv-sinage
   sudo ./install.sh
   ```

   It installs mpv and ffmpeg, creates a `signage` service user, sets up both
   services to start on boot, and asks you to choose the admin username and
   password (at least 8 characters).
3. Reboot if it asks you to: `sudo reboot`
4. Open `http://signage.local` (or the Pi's IP address) from a computer or phone
   on the same network and log in.

To update later:

```bash
cd ~/pi-tv-sinage && git pull && sudo ./install.sh
```


Your media, playlist and login are kept.

## Everyday commands

```bash
sudo signage-wifi                                # choose a Wi-Fi network (over SSH)
sudo signage-passwd                              # reset the admin login
sudo systemctl restart signage-player            # restart playback
sudo journalctl -u signage-player -f             # player logs
sudo journalctl -u signage-web -f                # web admin logs
sudo nano /etc/signage/signage.env               # settings (port, audio, upload limit...)
```

## Wi-Fi setup with just a keyboard

When the Pi starts, it waits up to 45 seconds for a saved Wi-Fi network or a
network cable. If neither connects, the TV shows a Wi-Fi setup screen: plug in
a USB keyboard, pick the network with the arrow keys and Enter, type the
password (Tab shows what you've typed) and press Enter. Once connected it shows
the admin address for a few seconds, then the signage starts. If a saved
network comes back on its own while the list is showing, it carries on by
itself. To change networks later, SSH in and run `sudo signage-wifi`.

## Settings file (`/etc/signage/signage.env`)

| Setting | Default | Notes |
|---|---|---|
| `SIGNAGE_PORT` | `80` | Web admin port |
| `SIGNAGE_MAX_UPLOAD_MB` | `8192` | Largest single upload |
| `SIGNAGE_HWDEC` | `drm` | Hardware decoding. `no` forces software. |
| `SIGNAGE_MPV_VO` | `--vo=gpu --gpu-context=drm` | Draw straight to HDMI. Use `--vo=gpu` if you keep the desktop running. |
| `SIGNAGE_AUDIO_DEVICE` | `alsa/hdmi:CARD=vc4hdmi0,DEV=0` | HDMI 0 is the port next to USB-C power. Use `vc4hdmi1` for the other port. `mpv --audio-device=help` lists devices. |
| `SIGNAGE_MPV_EXTRA` | (empty) | Extra mpv options, e.g. `--drm-mode=3840x2160@30` |

Restart both services after changing it.

## Troubleshooting

- **TV shows the console or nothing:** `sudo journalctl -u signage-player -n 50`.
  "Failed to acquire DRM master" or similar means a desktop is holding the
  screen: run `sudo systemctl set-default multi-user.target && sudo reboot`.
- **No sound:** plug the TV into HDMI 0 (next to the power socket) or change
  `SIGNAGE_AUDIO_DEVICE`, and check "Mute" is off in Settings.
- **4K video stutters:** check the item for a warning; convert it to HEVC as above.
- **Wrong resolution:** add e.g. `--drm-mode=3840x2160@30` to `SIGNAGE_MPV_EXTRA`.
- **Locked out:** after 5 wrong passwords, logins from that device are blocked
  for 5 minutes. `sudo signage-passwd` resets the login.

## Security notes

The admin page uses a hashed password, a session cookie that expires after 12
hours, CSRF protection, and a lockout after repeated wrong passwords. It's
served over plain HTTP, so keep it on your shop's private network and don't
forward port 80 from the internet. For remote access use a VPN such as
Tailscale.

## Development

```bash
pip install -r requirements.txt pytest
python -m pytest
SIGNAGE_DATA_DIR=./dev-data SIGNAGE_PORT=8080 python -m signage.cli set-password
SIGNAGE_DATA_DIR=./dev-data SIGNAGE_PORT=8080 python -m signage.web
```
