"""
Video AI Easy (videoaieasy.hdgr.online) — demo client, giống luồng XiaoYang web session.

Auth: Supabase password login → session cookie cho /api/*.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import time
from pathlib import Path

import requests

ORIGIN = os.environ.get("VIDEOAIEASY_ORIGIN", "https://videoaieasy.hdgr.online").rstrip("/")
SUPABASE_URL = os.environ.get(
    "VIDEOAIEASY_SUPABASE_URL", "https://gfevyulgkydodmlfnquh.supabase.co"
).rstrip("/")
SUPABASE_ANON_KEY = os.environ.get(
    "VIDEOAIEASY_SUPABASE_ANON_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImdmZXZ5dWxna3lkb2RtbGZucXVoIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODA0MTA2MjEsImV4cCI6MjA5NTk4NjYyMX0.8jSdH2RuxZnRUxHPI2MUSNvdx15A5ZfzE9kqT1YvfF0",
)
AUTH_COOKIE = "sb-gfevyulgkydodmlfnquh-auth-token"

MODEL_KLING_26 = "kling-2.6"
MODEL_KLING_30 = "kling-3.0"


class VideoAiEasyError(RuntimeError):
    pass


class VideoAiEasyAuthError(VideoAiEasyError):
    pass


def session_file_for_account(account_id: str) -> Path:
    safe = re.sub(r"[^a-z0-9_-]", "_", (account_id or "default").lower())
    return Path(__file__).resolve().parent / f"videoaieasy_session_{safe}.json"


def _encode_supabase_cookie(session: dict) -> str:
    payload = {
        "access_token": session["access_token"],
        "token_type": session.get("token_type", "bearer"),
        "expires_in": session.get("expires_in", 3600),
        "expires_at": session.get("expires_at"),
        "refresh_token": session.get("refresh_token"),
        "user": session.get("user"),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return "base64-" + base64.b64encode(raw).decode("ascii")


class VideoAiEasyClient:
    def __init__(self, account_id: str = "default", session: requests.Session | None = None):
        self.account_id = account_id
        self.session_file = session_file_for_account(account_id)
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": os.environ.get(
                    "VIDEOAIEASY_USER_AGENT",
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                ),
                "Accept": "application/json",
                "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
                "Origin": ORIGIN,
                "Referer": f"{ORIGIN}/dashboard",
            }
        )
        self._user_email: str | None = None
        self._load_session()

    def _save_session(self) -> None:
        data = {
            "cookie_name": AUTH_COOKIE,
            "cookie_value": self.session.cookies.get(AUTH_COOKIE, ""),
            "email": self._user_email,
        }
        self.session_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _load_session(self) -> None:
        if not self.session_file.is_file():
            return
        try:
            data = json.loads(self.session_file.read_text(encoding="utf-8"))
            name = data.get("cookie_name") or AUTH_COOKIE
            value = data.get("cookie_value") or ""
            if value:
                self.session.cookies.set(name, value, domain="videoaieasy.hdgr.online", path="/")
            self._user_email = data.get("email")
        except Exception:
            pass

    def _api(self, method: str, path: str, **kwargs) -> dict:
        timeout = kwargs.pop("timeout", 120)
        url = f"{ORIGIN}{path if path.startswith('/') else '/' + path}"
        r = self.session.request(method, url, timeout=timeout, **kwargs)
        if r.status_code == 401:
            raise VideoAiEasyAuthError("Session hết hạn — login lại")
        try:
            body = r.json() if r.content else {}
        except Exception:
            body = {"error": (r.text or "")[:500]}
        if not r.ok:
            err = body.get("error") if isinstance(body, dict) else None
            raise VideoAiEasyError(f"HTTP {r.status_code}: {err or (r.text or '')[:300]}")
        if isinstance(body, dict) and body.get("ok") is False:
            raise VideoAiEasyError(body.get("error") or "API lỗi")
        return body if isinstance(body, dict) else {"data": body}

    def login(self, email: str, password: str) -> dict:
        email = email.strip()
        if not email or not password:
            raise VideoAiEasyAuthError("Thiếu email/password")
        r = requests.post(
            f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
            headers={"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"},
            json={"email": email, "password": password},
            timeout=30,
        )
        if r.status_code != 200:
            detail = r.json().get("error_description") if r.content else r.text
            raise VideoAiEasyAuthError(f"Login thất bại: {detail or r.status_code}")
        sess = r.json()
        cookie_val = _encode_supabase_cookie(sess)
        self.session.cookies.set(AUTH_COOKIE, cookie_val, domain="videoaieasy.hdgr.online", path="/")
        self._user_email = email
        self._save_session()
        return sess

    def ensure_session(self, email: str, password: str) -> dict:
        try:
            profile = self.get_profile()
            return profile
        except (VideoAiEasyAuthError, VideoAiEasyError):
            self.login(email, password)
            return self.get_profile()

    def get_profile(self) -> dict:
        me = self._current_user()
        uid = me["id"]
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/profiles?select=*&id=eq.{uid}",
            headers={
                "apikey": SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {self._access_token()}",
            },
            timeout=30,
        )
        if r.status_code == 401:
            raise VideoAiEasyAuthError("Token hết hạn")
        rows = r.json() if r.content else []
        if not rows:
            raise VideoAiEasyError("Không tìm thấy profile")
        return rows[0]

    def _access_token(self) -> str:
        raw = self.session.cookies.get(AUTH_COOKIE, "")
        if not raw.startswith("base64-"):
            raise VideoAiEasyAuthError("Chưa có session cookie")
        payload = json.loads(base64.b64decode(raw[7:]).decode("utf-8"))
        token = payload.get("access_token")
        if not token:
            raise VideoAiEasyAuthError("Cookie không có access_token")
        return token

    def _current_user(self) -> dict:
        raw = self.session.cookies.get(AUTH_COOKIE, "")
        if not raw.startswith("base64-"):
            raise VideoAiEasyAuthError("Chưa đăng nhập")
        payload = json.loads(base64.b64decode(raw[7:]).decode("utf-8"))
        user = payload.get("user") or {}
        if not user.get("id"):
            raise VideoAiEasyAuthError("Cookie không hợp lệ")
        return user

    def coins_to_xu(self, coins: int | float) -> float:
        return round(float(coins) / 10, 1)

    def request_upload_url(
        self,
        *,
        kind: str,
        file_name: str,
        content_type: str,
        file_size: int,
    ) -> dict:
        body = self._api(
            "POST",
            "/api/upload",
            json={
                "kind": kind,
                "fileName": file_name,
                "contentType": content_type,
                "fileSize": file_size,
            },
            headers={"Content-Type": "application/json"},
        )
        return body["data"]

    def upload_bytes(
        self,
        *,
        kind: str,
        file_name: str,
        content_type: str,
        data: bytes,
    ) -> str:
        info = self.request_upload_url(
            kind=kind,
            file_name=file_name,
            content_type=content_type,
            file_size=len(data),
        )
        upload_url = (info.get("uploadUrl") or "").replace("\n", "").replace("\r", "").strip()
        public_url = re.sub(r"\s+", "", info.get("publicUrl") or "")
        r = requests.put(upload_url, data=data, headers={"Content-Type": content_type}, timeout=300)
        if not r.ok:
            raise VideoAiEasyError(f"Upload R2 HTTP {r.status_code}: {(r.text or '')[:200]}")
        if not public_url:
            raise VideoAiEasyError("Upload không trả publicUrl")
        return public_url

    def upload_file(self, file_path: str, *, kind: str | None = None) -> str:
        file_path = os.path.abspath(file_path)
        if not os.path.isfile(file_path):
            raise VideoAiEasyError(f"File không tồn tại: {file_path}")
        mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        if kind is None:
            kind = "video" if mime.startswith("video/") else "image"
        with open(file_path, "rb") as f:
            payload = f.read()
        return self.upload_bytes(
            kind=kind,
            file_name=os.path.basename(file_path),
            content_type=mime,
            data=payload,
        )

    def create_motion_job(
        self,
        *,
        input_image_url: str,
        driving_video_url: str,
        prompt: str = "",
        model_id: str = MODEL_KLING_26,
    ) -> str:
        body = {
            "mode": "motion-control",
            "modelId": model_id,
            "prompt": (prompt or "Follow the reference motion naturally").strip(),
            "inputImageUrl": input_image_url.strip(),
            "drivingVideoUrl": driving_video_url.strip(),
        }
        resp = self._api("POST", "/api/jobs", json=body, headers={"Content-Type": "application/json"})
        return str(resp["data"]["jobId"])

    def create_image_to_video_job(
        self,
        *,
        input_image_url: str,
        prompt: str = "",
        model_id: str = MODEL_KLING_26,
        duration_sec: int = 5,
        sound: bool = False,
    ) -> str:
        body = {
            "mode": "image-to-video",
            "modelId": model_id,
            "prompt": (prompt or "gentle natural motion").strip(),
            "durationSec": duration_sec,
            "sound": sound,
            "inputImageUrl": input_image_url.strip(),
        }
        resp = self._api("POST", "/api/jobs", json=body, headers={"Content-Type": "application/json"})
        return str(resp["data"]["jobId"])

    def get_job(self, job_id: str) -> dict:
        resp = self._api("GET", f"/api/jobs/{job_id}")
        return resp["data"]

    def list_jobs(self) -> list[dict]:
        resp = self._api("GET", "/api/jobs")
        return resp.get("data") or []

    def poll_job(
        self,
        job_id: str,
        *,
        interval_sec: float = 5.0,
        timeout_sec: float = 900.0,
        on_status=None,
    ) -> dict:
        deadline = time.time() + timeout_sec
        last = ""
        while time.time() < deadline:
            job = self.get_job(job_id)
            status = (job.get("status") or "").lower()
            if status != last:
                last = status
                if on_status:
                    on_status(job)
                else:
                    print(f"   status={status}")
            if status == "done":
                return job
            if status in ("failed", "expired"):
                raise VideoAiEasyError(job.get("error_message") or f"Job {status}")
            time.sleep(interval_sec)
        raise VideoAiEasyError(f"Poll timeout sau {timeout_sec}s")

    def download_job(self, job_id: str, dest_path: str) -> str:
        dest_path = os.path.abspath(dest_path)
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        url = f"{ORIGIN}/api/download/{job_id}"
        with self.session.get(url, stream=True, timeout=600) as r:
            if r.status_code == 401:
                raise VideoAiEasyAuthError("Session hết hạn khi tải video")
            if not r.ok:
                raise VideoAiEasyError(f"Download HTTP {r.status_code}: {(r.text or '')[:300]}")
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if chunk:
                        f.write(chunk)
        return dest_path
