"""Chuẩn hoá / cắt video tham chiếu trên server (bot VPS có ffmpeg)."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

try:
    import imageio_ffmpeg
except ImportError:
    imageio_ffmpeg = None  # type: ignore[assignment]

DEFAULT_MAX_SEC = 30.0


def max_reference_video_sec_for_order(order_data: dict | None) -> float:
    if not order_data:
        return DEFAULT_MAX_SEC
    for key in ("maxVideoSec", "vaeDurationSec", "durationSec"):
        val = order_data.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    return DEFAULT_MAX_SEC


def _ffmpeg_executable() -> str | None:
    if imageio_ffmpeg is not None:
        try:
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass
    import shutil

    return shutil.which("ffmpeg")


def probe_video_duration_seconds(path: str | Path) -> float | None:
    src = Path(path)
    if not src.is_file():
        return None
    try:
        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                str(src),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        data = json.loads(out.stdout)
        return float(data["format"]["duration"])
    except Exception:
        return None


def trim_video_to_seconds(source: Path, *, max_seconds: float, output: Path | None = None) -> Path:
    if not source.is_file():
        raise RuntimeError(f"File not found: {source}")

    ffmpeg = _ffmpeg_executable()
    if not ffmpeg:
        raise RuntimeError("Cần ffmpeg hoặc imageio-ffmpeg để cắt video")

    out_path = output
    if out_path is None:
        fd, path = tempfile.mkstemp(suffix=source.suffix or ".mp4")
        os.close(fd)
        out_path = Path(path)

    cmd = [ffmpeg, "-y", "-i", str(source), "-t", str(max_seconds), "-c", "copy", str(out_path)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(source),
            "-t",
            str(max_seconds),
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "18",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(out_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not out_path.is_file():
        raise RuntimeError(result.stderr.strip() or "ffmpeg trim failed")
    return out_path


def trim_reference_video_for_order(vid_path: str | Path, order_data: dict | None) -> str:
    """Cắt video tham chiếu về giới hạn gói nếu dài hơn (mobile upload không cắt được trên browser)."""
    path = Path(vid_path)
    max_sec = max_reference_video_sec_for_order(order_data)
    dur = probe_video_duration_seconds(path)
    if dur is None or dur <= max_sec + 0.15:
        return str(path)
    print(f"✂️ Server cắt video {dur:.1f}s → {max_sec:.0f}s")
    out = trim_video_to_seconds(path, max_seconds=max_sec)
    return str(out)
