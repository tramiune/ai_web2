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


def _wardrobe_replace_mode(cfg: dict | None = None) -> str:
    if cfg:
        mode = (cfg.get("wardrobeReplace") or "").strip()
        if mode:
            return mode
    return (get_env("XIAOYANG_WARDROBE_REPLACE") or "full").strip() or "full"


def _frame_seconds(cfg: dict | None = None) -> list[float]:
    """Ưu tiên frame muộn hơn để bắt cả áo + quần/váy (tránh chỉ thấy áo)."""
    base = 2.5
    if cfg:
        try:
            base = float(cfg.get("frameSec") or base)
        except (TypeError, ValueError):
            pass
    candidates = [base, base + 1.0, max(1.0, base - 0.5), 3.5]
    seen: set[float] = set()
    out: list[float] = []
    for s in candidates:
        k = round(s, 2)
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def run_wardrobe_web(
    client: XiaoyangWebClient,
    template_url: str,
    clothes_path: str,
    *,
    tmp: str,
    wardrobe_replace: str = "full",
) -> str:
    template_path = os.path.join(tmp, "batch_template.png")
    download_file(template_url, template_path, referer="https://nhay.cloud/")
    print(f"👗 Thay đồ XiaoYang (wardrobe_replace={wardrobe_replace})...")
    image_token = client.upload_file(template_path)
    clothes_token = client.upload_file(clothes_path)
    resp = client.create_wardrobe_task(
        image_token=image_token,
        clothes_image_token=clothes_token,
        wardrobe_replace=wardrobe_replace,
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
    source_video_id: str = "",
    source_order_id: str = "",
) -> str:
    ref = db.collection("orders").document()
    payload = {
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
        "createdAt": firestore.SERVER_TIMESTAMP,
        "updatedAt": firestore.SERVER_TIMESTAMP,
    }
    if source_video_id:
        payload["batchSourceVideoId"] = source_video_id
    if source_order_id:
        payload["batchSourceOrderId"] = source_order_id
    ref.set(payload)
    return ref.id


def _extract_outfit_frame(video_path: str, tmp: str, vid_key: str, cfg: dict) -> str:
    last_err: Exception | None = None
    for sec in _frame_seconds(cfg):
        frame_local = os.path.join(tmp, f"{vid_key}_t{sec}.png")
        try:
            extract_frame_at_sec(video_path, frame_local, sec)
            print(f"   🖼️ Frame t={sec}s")
            return frame_local
        except Exception as e:
            last_err = e
    raise RuntimeError(f"frame_extract_failed: {last_err}")


def _process_video_item(
    xy_client: XiaoyangWebClient,
    db,
    *,
    cfg: dict,
    template_url: str,
    admin_uid: str,
    admin_email: str,
    admin_name: str,
    run_ref,
    video_url: str,
    item_key: str,
    source_video_id: str = "",
    source_order_id: str = "",
    referer: str = "https://www.tiktok.com/",
) -> dict:
    wardrobe_mode = _wardrobe_replace_mode(cfg)
    item = {"videoId": item_key, "status": "pending", "orderId": ""}
    if source_order_id:
        item["sourceOrderId"] = source_order_id
    with tempfile.TemporaryDirectory(prefix="batch_ch_") as tmp:
        video_local = os.path.join(tmp, f"{item_key}.mp4")
        print(f"▶️ Nguồn {item_key}...")
        download_file(video_url, video_local, referer=referer)
        frame_local = _extract_outfit_frame(video_local, tmp, item_key, cfg)
        char_url = run_wardrobe_web(
            xy_client, template_url, frame_local, tmp=tmp, wardrobe_replace=wardrobe_mode,
        )
        motion_url = upload_motion_video(video_local)
        order_id = create_batch_order(
            db,
            admin_uid=admin_uid,
            admin_email=admin_email,
            admin_name=admin_name,
            char_url=char_url,
            video_url=motion_url,
            batch_run_id=run_ref.id,
            source_video_id=source_video_id,
            source_order_id=source_order_id,
        )
        item["status"] = "order_created"
        item["orderId"] = order_id
        item["characterImageLink"] = char_url
        item["referenceVideoLink"] = motion_url
        print(f"   ✅ Đơn {order_id}")
    return item


def upload_motion_video(local_path: str) -> str:
    url = upload_result_file(local_path, folder="motions", content_type="video/mp4")
    if not url:
        raise RuntimeError("upload motion video failed")
    return url


def _firestore_ts_seconds(ts) -> float:
    if ts is None:
        return 0.0
    if hasattr(ts, "timestamp"):
        return float(ts.timestamp())
    try:
        return float(ts)
    except (TypeError, ValueError):
        return 0.0


def poll_run_now_trigger() -> int:
    """Cron mỗi phút — chạy batch khi admin bấm「Chạy thử ngay」trên web."""
    if not firebase_admin._apps:
        cred = credentials.Certificate(str(ROOT / "serviceAccountKey.json"))
        firebase_admin.initialize_app(cred)
    db = firestore.client()
    cfg_ref = db.collection(CONFIG_COLLECTION).document(CONFIG_DOC)
    cfg_snap = cfg_ref.get()
    if not cfg_snap.exists:
        return 0
    cfg = cfg_snap.to_dict() or {}
    requested = cfg.get("runNowRequestedAt")
    if not requested:
        return 0
    handled = cfg.get("runNowHandledAt")
    if _firestore_ts_seconds(handled) >= _firestore_ts_seconds(requested):
        return 0
    running = (
        db.collection(RUNS_COLLECTION)
        .where("status", "==", "running")
        .limit(1)
        .stream()
    )
    if any(True for _ in running):
        print("⏳ Batch đang chạy — bỏ qua trigger mới.")
        return 0
    mode = (cfg.get("runNowMode") or "test").strip().lower()
    cfg_ref.update({"runNowHandledAt": requested})
    order_ids = [str(x).strip() for x in (cfg.get("selectedOrderIds") or []) if str(x).strip()]
    if mode == "orders":
        print(f"🚀 Trigger「Làm ngay / copy đơn」— {len(order_ids)} đơn.")
        return run_batch(force=True, manual=True, source_mode="orders", order_ids=order_ids)
    if mode == "full":
        print("🚀 Trigger「Làm ngay」— batch đầy đủ (video hôm qua).")
        return run_batch(force=True, manual=True)
    print("🚀 Trigger「Chạy thử」— 1 video mới nhất.")
    return run_batch(force=True, test_latest=1, manual=True)


def run_daily_hourly() -> int:
    """Cron mỗi giờ — chạy khi đúng cronHour (VN) và enabled."""
    if not firebase_admin._apps:
        cred = credentials.Certificate(str(ROOT / "serviceAccountKey.json"))
        firebase_admin.initialize_app(cred)
    db = firestore.client()
    cfg_ref = db.collection(CONFIG_COLLECTION).document(CONFIG_DOC)
    cfg_snap = cfg_ref.get()
    if not cfg_snap.exists:
        return 0
    cfg = cfg_snap.to_dict() or {}
    if not cfg.get("enabled"):
        return 0
    try:
        cron_hour = int(cfg.get("cronHour") if cfg.get("cronHour") is not None else 3)
    except (TypeError, ValueError):
        cron_hour = 3
    cron_hour = max(0, min(23, cron_hour))
    if _vn_now().hour != cron_hour:
        return 0
    y_date = (_vn_now().date() - timedelta(days=1)).isoformat()
    if cfg.get("lastDailyCronDateVN") == y_date:
        return 0
    print(f"⏰ Cron batch kênh — {cron_hour}:00 VN, ngày video {y_date}")
    rc = run_batch(force=False, manual=False)
    if rc == 0:
        cfg_ref.update({"lastDailyCronDateVN": y_date})
    return rc


def run_batch(
    *,
    force: bool = False,
    test_latest: int | None = None,
    manual: bool = False,
    source_mode: str | None = None,
    order_ids: list[str] | None = None,
) -> int:
    if not firebase_admin._apps:
        cred = credentials.Certificate(str(ROOT / "serviceAccountKey.json"))
        firebase_admin.initialize_app(cred)
    db = firestore.client()

    cfg_snap = db.collection(CONFIG_COLLECTION).document(CONFIG_DOC).get()
    if not cfg_snap.exists:
        print("⏭️ Chưa có batchChannelConfig — bỏ qua.")
        return 0
    cfg = cfg_snap.to_dict() or {}
    if not manual and not cfg.get("enabled"):
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
    source_mode = (source_mode or cfg.get("sourceMode") or "tiktok").strip().lower()
    if not template_url or not admin_uid:
        print("❌ Config thiếu templateImageUrl / createdBy")
        return 1
    if source_mode != "orders" and not channel:
        print("❌ Config thiếu channel (chế độ TikTok)")
        return 1

    if test_latest and test_latest > 0:
        y_date = f"test-{_vn_now().date().isoformat()}"
    else:
        y_date = (_vn_now().date() - timedelta(days=1)).isoformat()
    if not force and not manual:
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

    username = parse_tiktok_username(channel) if channel else ""
    run_ref = db.collection(RUNS_COLLECTION).document()
    run_ref.set({
        "dateVN": y_date,
        "channelUsername": username,
        "sourceMode": source_mode,
        "status": "running",
        "isManualTest": bool(manual or test_latest),
        "testLatest": int(test_latest or 0),
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
        if source_mode == "orders":
            ids = order_ids if order_ids is not None else [
                str(x).strip() for x in (cfg.get("selectedOrderIds") or []) if str(x).strip()
            ]
            if not ids:
                raise RuntimeError("Chưa chọn đơn nguồn (selectedOrderIds)")
            print(f"📋 Copy {len(ids)} đơn có ảnh + video...")
            run_ref.update({"videosFound": len(ids)})
            for oid in ids:
                snap = db.collection("orders").document(oid).get()
                if not snap.exists:
                    errors.append(f"{oid}: not_found")
                    items.append({"videoId": oid, "status": "error", "error": "not_found", "orderId": ""})
                    run_ref.update({"items": items, "ordersCreated": orders_created, "errors": errors})
                    continue
                od = snap.to_dict() or {}
                video_url = (od.get("referenceVideoLink") or "").strip()
                if not video_url:
                    errors.append(f"{oid}: no_reference_video")
                    items.append({"videoId": oid, "status": "error", "error": "no_reference_video", "orderId": ""})
                    run_ref.update({"items": items, "ordersCreated": orders_created, "errors": errors})
                    continue
                try:
                    item = _process_video_item(
                        xy_client, db,
                        cfg=cfg,
                        template_url=template_url,
                        admin_uid=admin_uid,
                        admin_email=admin_email,
                        admin_name=admin_name,
                        run_ref=run_ref,
                        video_url=video_url,
                        item_key=oid[-8:],
                        source_order_id=oid,
                        referer="https://nhay.cloud/",
                    )
                    orders_created += 1
                except Exception as e:
                    msg = f"{oid}: {e}"
                    print(f"   ❌ {msg}")
                    errors.append(msg)
                    item = {"videoId": oid, "sourceOrderId": oid, "status": "error", "error": str(e), "orderId": ""}
                items.append(item)
                run_ref.update({"items": items, "ordersCreated": orders_created, "errors": errors})
        else:
            if test_latest and test_latest > 0:
                print(f"📡 Lấy {test_latest} video mới nhất @{username} (chạy thử)...")
                all_videos = fetch_channel_videos(username)
                videos = all_videos[:test_latest]
                print(f"   Chọn {len(videos)} video (trong {len(all_videos)} gần nhất).")
            else:
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
                try:
                    item = _process_video_item(
                        xy_client, db,
                        cfg=cfg,
                        template_url=template_url,
                        admin_uid=admin_uid,
                        admin_email=admin_email,
                        admin_name=admin_name,
                        run_ref=run_ref,
                        video_url=play,
                        item_key=vid,
                        source_video_id=vid,
                    )
                    orders_created += 1
                except Exception as e:
                    msg = f"{vid}: {e}"
                    print(f"   ❌ {msg}")
                    errors.append(msg)
                    item = {"videoId": vid, "status": "error", "error": str(e), "orderId": ""}
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
    parser.add_argument(
        "--test-latest",
        type=int,
        default=0,
        metavar="N",
        help="Chạy thử: lấy N video mới nhất thay vì chỉ hôm qua",
    )
    parser.add_argument(
        "--poll-trigger",
        action="store_true",
        help="Kiểm tra runNowRequestedAt trên Firestore (cron mỗi phút)",
    )
    parser.add_argument(
        "--daily-hourly",
        action="store_true",
        help="Cron mỗi giờ — chạy khi đúng cronHour trong batchChannelConfig",
    )
    args = parser.parse_args()
    if args.poll_trigger:
        sys.exit(poll_run_now_trigger())
    if args.daily_hourly:
        sys.exit(run_daily_hourly())
    test_latest = args.test_latest if args.test_latest > 0 else None
    sys.exit(run_batch(force=args.force or bool(test_latest), test_latest=test_latest))


if __name__ == "__main__":
    main()
