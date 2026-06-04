"""Chuẩn hóa URL media cho XiaoYang API (Workers CDN + kiểm tra trước khi gửi)."""

from __future__ import annotations

import re
import struct
import time
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests

WORKER_HOST = "motionai-upload-api.traderfinn0312.workers.dev"
WORKER_BASE = f"https://{WORKER_HOST}"
MOTION_VIDEO_MIN_SEC = float(__import__("os").environ.get("XIAOYANG_MOTION_MIN_VIDEO_SEC", "5"))
MOTION_VIDEO_MAX_SEC = float(__import__("os").environ.get("XIAOYANG_MOTION_MAX_VIDEO_SEC", "30"))


class MediaValidationError(ValueError):
    """Ảnh/video không đạt yêu cầu Motion Control."""


def is_workers_query_url(url: str) -> bool:
    """Link MotionAI Workers ?file=... — API v1 XiaoYang thường từ chối (không phải direct file URL)."""
    try:
        u = urlparse((url or "").strip())
        return WORKER_HOST in (u.hostname or "").lower() and "file=" in (u.query or "")
    except Exception:
        return False


def normalize_public_media_url(url: str, *, for_api_v1: bool = False) -> str:
    """Workers.dev: thêm download=1 khi bot tải file. API v1 cần URL direct — không dùng ?file=."""
    url = (url or "").strip()
    if not url:
        return url
    if for_api_v1 and is_workers_query_url(url):
        raise MediaValidationError(
            "Link Workers ?file=... không dùng được với API v1 image_url/video_url. "
            "Dùng luồng web: XIAOYANG_COOKIE + xiaoyang_test.py menu 3b, hoặc URL file trực tiếp "
            "(vd https://cdn.com/video.mp4 không có ?file=)."
        )
    parsed = urlparse(url)
    if WORKER_HOST not in (parsed.hostname or "").lower():
        return url
    q = parse_qs(parsed.query, keep_blank_values=True)
    if "download" not in q:
        q["download"] = ["1"]
    new_query = urlencode([(k, v[0]) for k, vals in q.items() for v in [vals]], doseq=False)
    return urlunparse(parsed._replace(query=new_query))


def _mp4_duration_sec(data: bytes) -> float | None:
    """Đọc mvhd trong moov — không dùng find() (dễ trúng bytes ngẫu nhiên trong mdat)."""

    def _mvhd_at(pos: int) -> float | None:
        if pos + 32 > len(data) or data[pos + 4 : pos + 8] != b"mvhd":
            return None
        ver = data[pos + 8]
        if ver == 0:
            timescale = struct.unpack(">I", data[pos + 20 : pos + 24])[0]
            duration = struct.unpack(">I", data[pos + 24 : pos + 28])[0]
        else:
            timescale = struct.unpack(">I", data[pos + 28 : pos + 32])[0]
            duration = struct.unpack(">Q", data[pos + 32 : pos + 40])[0]
        if not timescale:
            return None
        return duration / timescale

    def _walk(start: int, end: int) -> float | None:
        pos = start
        while pos + 8 <= end:
            size = struct.unpack(">I", data[pos : pos + 4])[0]
            typ = data[pos + 4 : pos + 8]
            if size < 8:
                break
            if size == 0:
                size = end - pos
            box_end = pos + size
            if typ == b"mvhd":
                return _mvhd_at(pos)
            if typ in (b"moov", b"trak", b"mdia", b"meta"):
                found = _walk(pos + 8, box_end)
                if found is not None:
                    return found
            pos = box_end
        return None

    return _walk(0, len(data))


def _png_size(data: bytes) -> tuple[int, int] | None:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    w, h = struct.unpack(">II", data[16:24])
    return w, h


def probe_media_url(url: str, timeout: int = 60) -> dict:
    """GET URL và trả metadata cơ bản."""
    url = normalize_public_media_url(url)
    r = requests.get(
        url,
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0 (compatible; XiaoyangBot/1.0)"},
        stream=True,
    )
    r.raise_for_status()
    # Đọc tối đa ~12MB để đo duration PNG/MP4 header
    chunks = []
    size = 0
    for chunk in r.iter_content(256 * 1024):
        if not chunk:
            continue
        chunks.append(chunk)
        size += len(chunk)
        if size >= 12 * 1024 * 1024:
            break
    data = b"".join(chunks)
    ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    info = {"url": url, "content_type": ctype, "bytes": size, "status": r.status_code}
    if "png" in ctype or data[:8] == b"\x89PNG\r\n\x1a\n":
        info["kind"] = "image"
        wh = _png_size(data)
        if wh:
            info["width"], info["height"] = wh
    elif "video" in ctype or data[4:8] == b"ftyp":
        info["kind"] = "video"
        dur = _mp4_duration_sec(data)
        if dur is not None:
            info["duration_sec"] = round(dur, 2)
    else:
        info["kind"] = "unknown"
    return info


def validate_motion_media(image_url: str, video_url: str, *, for_api_v1: bool = True) -> tuple[str, str]:
    """
    Chuẩn hóa URL + kiểm tra trước POST /api/v1/tasks.
    Trả (image_url, video_url) đã normalize.
    """
    image_url = normalize_public_media_url(image_url, for_api_v1=for_api_v1)
    video_url = normalize_public_media_url(video_url, for_api_v1=for_api_v1)
    img = probe_media_url(image_url)
    vid = probe_media_url(video_url)

    if img.get("kind") != "image":
        raise MediaValidationError(f"image_url không phải ảnh hợp lệ: {img}")
    if vid.get("kind") != "video":
        raise MediaValidationError(f"video_url không phải video MP4 hợp lệ: {vid}")

    dur = vid.get("duration_sec")
    if dur is not None and dur < MOTION_VIDEO_MIN_SEC:
        raise MediaValidationError(
            f"Video motion quá ngắn ({dur}s). Kling Motion cần khoảng "
            f"{MOTION_VIDEO_MIN_SEC:.0f}–{MOTION_VIDEO_MAX_SEC:.0f}s, có người và chuyển động rõ."
        )
    if dur is not None and dur > MOTION_VIDEO_MAX_SEC:
        raise MediaValidationError(
            f"Video motion quá dài ({dur}s). Tối đa ~{MOTION_VIDEO_MAX_SEC:.0f}s (theo XiaoYang UI)."
        )
    return image_url, video_url


def reupload_to_worker(
    data: bytes,
    folder: str,
    filename: str,
    content_type: str,
    worker_base: str = WORKER_BASE,
) -> str:
    """POST binary lên Workers (giống MotionAI bot) → URL public."""
    import requests.utils

    safe = re.sub(r"[^\w.\-]+", "_", filename) or "file.bin"
    key = f"{folder}/{int(time.time() * 1000)}_{safe}"
    post_url = f"{worker_base}/?{urlencode({'file': key, 't': int(time.time() * 1000)})}"
    r = requests.post(
        post_url,
        data=data,
        headers={"Content-Type": content_type},
        timeout=120,
    )
    r.raise_for_status()
    body = r.json()
    url = body.get("url") or ""
    if not url:
        raise RuntimeError(f"Worker upload không trả url: {body}")
    return normalize_public_media_url(url)
