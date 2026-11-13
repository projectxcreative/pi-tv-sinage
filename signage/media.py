"""Inspecting, normalising and thumbnailing uploaded media."""
import json
import logging
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageOps

log = logging.getLogger(__name__)

VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".ts", ".avi"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

# Largest picture worth keeping: a 4K screen. Bigger photos are scaled down
# on upload so they load fast and don't waste memory on the Pi.
MAX_IMAGE_SIZE = (3840, 2160)

# What the Raspberry Pi 4B can decode in hardware.
#  - HEVC / H.265: up to 4K (2160p60)
#  - H.264: up to 1080p60 only
# Anything else is decoded on the CPU, which is fine for small videos but
# stutters at high resolutions.
HW_LIMITS = {"hevc": (4096, 2304), "h264": (1920, 1088)}


def kind_for(filename):
    ext = Path(filename).suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    return None


def probe(path):
    """Return (codec, width, height, length_seconds) using ffprobe, or Nones."""
    if not shutil.which("ffprobe"):
        return None, None, None, None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height:format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout
        data = json.loads(out)
    except (subprocess.SubprocessError, ValueError, OSError) as exc:
        log.warning("ffprobe failed for %s: %s", path, exc)
        return None, None, None, None
    stream = (data.get("streams") or [{}])[0]
    length = data.get("format", {}).get("duration")
    try:
        length = float(length) if length is not None else None
    except ValueError:
        length = None
    return stream.get("codec_name"), stream.get("width"), stream.get("height"), length


def playback_warning(codec, width, height):
    """Explain when a video won't play smoothly on a Pi 4B, else None."""
    if not codec:
        return "Could not read this video. It may not play."
    w, h = width or 0, height or 0
    big = max(w, h) > 1920 or min(w, h) > 1088
    if codec == "h264" and big:
        return ("This is 4K H.264. The Pi 4 can only hardware-decode H.264 up to 1080p, "
                "so it will stutter. Re-encode it to H.265 (HEVC) for smooth 4K.")
    if codec not in HW_LIMITS and big:
        return (f"This video uses {codec.upper()}, which the Pi 4 decodes in software. "
                "Above 1080p it will stutter. Re-encode it to H.265 (HEVC).")
    if codec == "hevc" and (w > 4096 or h > 2304):
        return "This video is larger than 4K and will not play smoothly."
    return None


def normalise_image(path):
    """Apply EXIF rotation and shrink oversized photos in place.

    Returns (width, height). Animated GIFs are left untouched.
    """
    with Image.open(path) as img:
        if getattr(img, "is_animated", False):
            return img.size
        fmt = img.format
        fixed = ImageOps.exif_transpose(img)
        changed = fixed is not img
        if fixed.width > MAX_IMAGE_SIZE[0] or fixed.height > MAX_IMAGE_SIZE[1]:
            fixed = fixed.copy()
            fixed.thumbnail(MAX_IMAGE_SIZE, Image.LANCZOS)
            changed = True
        size = fixed.size
        if changed:
            if fmt == "JPEG" and fixed.mode not in ("RGB", "L"):
                fixed = fixed.convert("RGB")
            save_kwargs = {"quality": 92} if fmt == "JPEG" else {}
            fixed.save(path, format=fmt, **save_kwargs)
        return size


def make_thumbnail(src, dest, kind, width=480):
    """Write a JPEG thumbnail. Returns True on success."""
    dest = Path(dest)
    try:
        if kind == "image":
            with Image.open(src) as img:
                img = ImageOps.exif_transpose(img).convert("RGB")
                img.thumbnail((width, width))
                img.save(dest, "JPEG", quality=80)
            return True
        if not shutil.which("ffmpeg"):
            return False
        for seek in ("1", "0"):
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-ss", seek, "-i", str(src),
                 "-frames:v", "1", "-vf", f"scale={width}:-2", str(dest)],
                capture_output=True, timeout=120, check=False,
            )
            if dest.exists() and dest.stat().st_size > 0:
                return True
    except Exception as exc:  # thumbnails are nice-to-have
        log.warning("thumbnail failed for %s: %s", src, exc)
    return False
