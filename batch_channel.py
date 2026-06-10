#!/usr/bin/env python3
"""
Batch kênh TikTok (Nhay Cloud) — chạy cron 3:00 Asia/Ho_Chi_Minh.

Pipeline mỗi video đăng ngày hôm qua (VN):
  tải video → cắt frame t=1s → XiaoYang thay đồ → tạo đơn motion pending.

Cần .env: XIAOYANG_ACCOUNTS (nick web như bot) + R2_* + serviceAccountKey.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
import firebase_admin
from firebase_admin import credentials, firestore

from project_env import get_env, load_project_env

load_project_env()

from xiaoyang_direct import DirectMediaError, upload_result_file
from xiaoyang_web import XiaoyangAuthError, XiaoyangWebClient, XiaoyangWebError

ROOT = Path(__file__).resolve().parent
CONFIG_DOC = "default"
CONFIG_COLLECTION = "batchChannelConfig"
RUNS_COLLECTION = "batchChannelRuns"
TIKWM_USER_POSTS = "https://www.tikwm.com/api/user/posts"
VN_TZ = "Asia/Ho_Chi_Minh"

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore


def _vn_now() -> datetime:
    if ZoneInfo:
        return datetime.now(ZoneInfo(VN_TZ))
    return datetime.utcnow() + timedelta(hours=7)


def _yesterday_vn_range() -> tuple[int, int]:
    """Unix seconds [start, end) for yesterday 00:00–24:00 VN."""
    now = _vn_now()
    y = (now.date() - timedelta(days=1))
    if ZoneInfo:
        z = ZoneInfo(VN_TZ)
        start = int(datetime(y.year, y.month, y.day, 0, 0, 0, tzinfo=z).timestamp())
        end = int(datetime(y.year, y.month, y.day, 23, 59, 59, tzinfo=z).timestamp()) + 1
    else:
        start = int(datetime(y.year, y.month, y.day, 0, 0, 0).timestamp()) - 7 * 3600
        end = start + 86400
    return start, end


def parse_tiktok_username(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        raise ValueError("empty_channel")
    if s.startswith("@"):
        return s[1:].split("/")[0].strip()
    if "tiktok.com" in s:
        m = re.search(r"tiktok\.com/@([^/?#]+)", s, re.I)
        if m:
            return m.group(1).strip()
    return s.split("/")[0].strip().lstrip("@")


def fetch_channel_videos(username: str, *, max_pages: int = 5) -> list[dict]:
    username = parse_tiktok_username(username)
    videos: list[dict] = []
    cursor = 0
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    for _ in range(max_pages):
        r = requests.get(
            TIKWM_USER_POSTS,
            params={"unique_id": username, "count": 30, "cursor": cursor},
            headers=headers,
            timeout=60,
        )
        r.raise_for_status()
        payload = r.json()
        if payload.get("code") != 0:
            raise RuntimeError(f"tikwm: {payload.get('msg') or payload}")
        data = payload.get("data") or {}
        batch = data.get("videos") or []
        if not batch:
            break
        videos.extend(batch)
        if not data.get("hasMore"):
            break
        cursor = int(data.get("cursor") or 0)
        time.sleep(0.4)
    return videos


def filter_videos_yesterday(videos: list[dict]) -> list[dict]:
    start, end = _yesterday_vn_range()
    out = []
    for v in videos:
        ts = int(v.get("create_time") or 0)
        if start <= ts < end:
            out.append(v)
    return out


def _xiaoyang_account_id(email: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "_", (email or "default").split("@")[0].lower())


def load_xy_accounts() -> list[dict]:
    """Cùng format bot.py — XIAOYANG_ACCOUNTS hoặc XIAOYANG_EMAIL/PASSWORD."""
    accounts: list[dict] = []
    raw = (get_env("XIAOYANG_ACCOUNTS") or "").strip()
    if raw:
        if raw.startswith("["):
            try:
                for item in json.loads(raw):
                    email = (item.get("email") or "").strip()
                    password = item.get("password") or ""
                    if email and password:
                        accounts.append({
                            "id": _xiaoyang_account_id(email),
                            "email": email,
                            "password": password,
                        })
            except Exception as e:
                print(f"⚠️ XIAOYANG_ACCOUNTS JSON lỗi: {e}")
        else:
            for part in raw.split(","):
                part = part.strip()
                if ":" not in part:
                    continue
                email, password = part.split(":", 1)
                email, password = email.strip(), password.strip()
                if email and password:
                    accounts.append({
                        "id": _xiaoyang_account_id(email),
                        "email": email,
                        "password": password,
                    })
    if not accounts:
        email = (get_env("XIAOYANG_EMAIL") or "").strip()
        password = get_env("XIAOYANG_PASSWORD") or ""
        if email and password:
            accounts.append({
                "id": _xiaoyang_account_id(email),
                "email": email,
                "password": password,
            })
    return accounts


def get_batch_xy_client() -> tuple[XiaoyangWebClient, dict]:
    accounts = load_xy_accounts()
    if not accounts:
        raise RuntimeError("Thiếu XIAOYANG_ACCOUNTS / XIAOYANG_EMAIL trong .env (nick web bot)")
    acc = accounts[0]
    client = XiaoyangWebClient(account_id=acc["id"])
    try:
        client.me()
    except XiaoyangAuthError:
        client.login(email=acc["email"], password=acc["password"])
        client.me()
    print(f"🔑 XiaoYang web: {acc['email']} ({acc['id']})")
    return client, acc


def download_file(url: str, dest: str, *, referer: str = "https://www.tiktok.com/") -> str:
    dest = os.path.abspath(dest)
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    with requests.get(url, stream=True, timeout=600, headers={"Referer": referer}) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(256 * 1024):
                if chunk:
                    f.write(chunk)
    return dest


def download_video(url: str, dest: str) -> str:
    return download_file(url, dest, referer="https://www.tiktok.com/")


def extract_frame_at_sec(video_path: str, out_image: str, sec: float = 1.0) -> str:
    out_image = os.path.abspath(out_image)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", str(sec), "-i", video_path,
        "-frames:v", "1", "-q:v", "2", out_image,
    ]
    subprocess.run(cmd, check=True, timeout=120)
    if not os.path.isfile(out_image) or os.path.getsize(out_image) < 100:
        raise RuntimeError("frame_extract_failed")
    return out_image


def poll_xy_task(client, task_id: str, *, label: str, timeout_sec: int = 1800) -> dict:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        t = client.get_task(task_id)
        st = (t.get("status") or "").upper()
        print(f"   [{label}] task {task_id}: {st}")
        if st == "SUCCESS":
            return t
        if st == "FAIL":
            raise RuntimeError(t.get("error_message") or f"task {task_id} FAIL")
        time.sleep(20)
    raise TimeoutError(f"task {task_id} timeout")


def run_wardrobe_web(
    client: XiaoyangWebClient,
    template_url: str,
    clothes_path: str,
    *,
    tmp: str,
) -> str:
    template_path = os.path.join(tmp, "batch_template.png")
    download_file(template_url, template_path, referer="https://nhay.cloud/")
    print("👗 Thay đồ XiaoYang (web session)...")
    image_token = client.upload_file(template_path)
    clothes_token = client.upload_file(clothes_path)
    resp = client.create_wardrobe_task(
        image_token=image_token,
        clothes_image_token=clothes_token,
    )
    task_id = str(resp.get("task_id") or "").strip()
    if not task_id:
        raise RuntimeError(f"wardrobe no task_id: {resp}")
    poll_xy_task(client, task_id, label="wardrobe")
    out_path = os.path.join(tmp, f"wardrobe_{task_id}.png")
    client.download_task_file(task_id, out_path)
    url = upload_result_file(out_path, folder="characters", content_type="image/png")
    if not url:
        raise RuntimeError("upload wardrobe result failed")
    return url


def create_batch_order(
    db,
    *,
    admin_uid: str,
    admin_email: str,
    admin_name: str,
    char_url: str,
    video_url: str,
    batch_run_id: str,
    source_video_id: str,
) -> str:
    ref = db.collection("orders").document()
    ref.set({
        "userId": admin_uid,
        "userEmail": admin_email or "",
        "userName": admin_name or "Admin",
        "packageName": "Batch kênh TikTok",
        "modelId": "124",
        "serviceType": "motion-to-char",
        "serviceLabel": "AI Copy Chuyển Động Vào Ảnh (30s)",
        "costCoins": 0,
        "characterImageLink": char_url,
        "referenceVideoLink": video_url,
        "aspectRatio": "9:16",
        "status": "pending",
        "resultLink": "",
        "adminNote": "batch-channel",
        "isBatchChannel": True,
        "batchChannelRunId": batch_run_id,
        "batchSourceVideoId": source_video_id,
        "createdAt": firestore.SERVER_TIMESTAMP,
        "updatedAt": firestore.SERVER_TIMESTAMP,
    })
    return ref.id


def upload_motion_video(local_path: str) -> str:
    url = upload_result_file(local_path, folder="motions", content_type="video/mp4")
    if not url:
        raise RuntimeError("upload motion video failed")
    return url


def run_batch(*, force: bool = False) -> int:
    if not firebase_admin._apps:
        cred = credentials.Certificate(str(ROOT / "serviceAccountKey.json"))
        firebase_admin.initialize_app(cred)
    db = firestore.client()

    cfg_snap = db.collection(CONFIG_COLLECTION).document(CONFIG_DOC).get()
    if not cfg_snap.exists:
        print("⏭️ Chưa có batchChannelConfig — bỏ qua.")
        return 0
    cfg = cfg_snap.to_dict() or {}
    if not cfg.get("enabled"):
        print("⏭️ Batch kênh đang tắt (enabled=false).")
        return 0

    try:
        xy_client, xy_acc = get_batch_xy_client()
    except (XiaoyangWebError, XiaoyangAuthError, RuntimeError) as e:
        print(f"❌ Không đăng nhập XiaoYang web: {e}")
        return 1

    template_url = (cfg.get("templateImageUrl") or "").strip()
    channel = (cfg.get("channelUsername") or cfg.get("channelUrl") or "").strip()
    admin_uid = (cfg.get("createdBy") or "").strip()
    admin_email = (cfg.get("createdByEmail") or "").strip()
    admin_name = (cfg.get("createdByName") or "Admin").strip()
    if not template_url or not channel or not admin_uid:
        print("❌ Config thiếu templateImageUrl / channel / createdBy")
        return 1

    y_date = (_vn_now().date() - timedelta(days=1)).isoformat()
    if not force:
        recent = (
            db.collection(RUNS_COLLECTION)
            .where("dateVN", "==", y_date)
            .where("status", "==", "completed")
            .limit(1)
            .stream()
        )
        if any(True for _ in recent):
            print(f"⏭️ Đã chạy batch cho ngày {y_date}.")
            return 0

    username = parse_tiktok_username(channel)
    run_ref = db.collection(RUNS_COLLECTION).document()
    run_ref.set({
        "dateVN": y_date,
        "channelUsername": username,
        "status": "running",
        "startedAt": firestore.SERVER_TIMESTAMP,
        "videosFound": 0,
        "ordersCreated": 0,
        "items": [],
        "errors": [],
    })

    errors: list[str] = []
    items: list[dict] = []
    orders_created = 0

    try:
        print(f"📡 Lấy video @{username} — ngày hôm qua VN ({y_date})...")
        all_videos = fetch_channel_videos(username)
        videos = filter_videos_yesterday(all_videos)
        print(f"   Tìm thấy {len(videos)} video hôm qua (trong {len(all_videos)} gần nhất).")
        run_ref.update({"videosFound": len(videos)})

        for v in videos:
            vid = str(v.get("video_id") or "")
            play = v.get("hdplay") or v.get("play") or ""
            if not vid or not play:
                errors.append(f"{vid or '?'}: no_play_url")
                continue
            item = {"videoId": vid, "status": "pending", "orderId": ""}
            try:
                with tempfile.TemporaryDirectory(prefix="batch_ch_") as tmp:
                    video_local = os.path.join(tmp, f"{vid}.mp4")
                    frame_local = os.path.join(tmp, f"{vid}_t1.png")
                    print(f"▶️ Video {vid}...")
                    download_video(play, video_local)
                    extract_frame_at_sec(video_local, frame_local, 1.0)
                    char_url = run_wardrobe_web(xy_client, template_url, frame_local, tmp=tmp)
                    motion_url = upload_motion_video(video_local)
                    order_id = create_batch_order(
                        db,
                        admin_uid=admin_uid,
                        admin_email=admin_email,
                        admin_name=admin_name,
                        char_url=char_url,
                        video_url=motion_url,
                        batch_run_id=run_ref.id,
                        source_video_id=vid,
                    )
                    item["status"] = "order_created"
                    item["orderId"] = order_id
                    orders_created += 1
                    print(f"   ✅ Đơn {order_id}")
            except Exception as e:
                msg = f"{vid}: {e}"
                print(f"   ❌ {msg}")
                errors.append(msg)
                item["status"] = "error"
                item["error"] = str(e)
            items.append(item)
            run_ref.update({"items": items, "ordersCreated": orders_created, "errors": errors})

        run_ref.update({
            "status": "completed",
            "finishedAt": firestore.SERVER_TIMESTAMP,
            "ordersCreated": orders_created,
            "errors": errors,
            "items": items,
        })
        db.collection(CONFIG_COLLECTION).document(CONFIG_DOC).update({
            "lastRunAt": firestore.SERVER_TIMESTAMP,
            "lastRunStatus": "completed",
            "lastRunMessage": f"{orders_created} đơn, {len(errors)} lỗi",
        })
        print(f"✅ Batch xong: {orders_created} đơn pending, {len(errors)} lỗi.")
        return 0 if not errors else 0
    except Exception as e:
        print(f"❌ Batch thất bại: {e}")
        run_ref.update({
            "status": "failed",
            "finishedAt": firestore.SERVER_TIMESTAMP,
            "errors": errors + [str(e)],
        })
        db.collection(CONFIG_COLLECTION).document(CONFIG_DOC).update({
            "lastRunAt": firestore.SERVER_TIMESTAMP,
            "lastRunStatus": "failed",
            "lastRunMessage": str(e),
        })
        return 1


def main():
    parser = argparse.ArgumentParser(description="Nhay Cloud — batch kênh TikTok")
    parser.add_argument("--force", action="store_true", help="Chạy lại dù đã có run completed hôm qua")
    args = parser.parse_args()
    sys.exit(run_batch(force=args.force))


if __name__ == "__main__":
    main()
