import time
import os
import sys
import re
import base64
import argparse
import socket
import queue
import requests
import firebase_admin
import threading
from datetime import datetime, timezone
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from playwright.sync_api import sync_playwright

from project_env import get_env, load_project_env

load_project_env()

from aidancing_api import AidancingApiClient, SessionExpiredError
from xiaoyang_api import XiaoyangApiClient, XiaoyangAuthError as XiaoyangApiAuthError, XiaoyangApiError
from xiaoyang_direct import DirectMediaError, upload_result_file
from xiaoyang_media import MediaValidationError
from xiaoyang_web import XiaoyangWebClient, XiaoyangAuthError as XiaoyangWebAuthError, XiaoyangWebError
from videoaieasy_web import (
    VideoAiEasyClient,
    VideoAiEasyAuthError,
    VideoAiEasyError,
    MODEL_KLING_26,
    MODEL_KLING_30,
    prepare_character_image_for_vae,
    resolution_for_order,
    duration_for_order,
)

# --- CONFIGURATION ---
cred = credentials.Certificate("serviceAccountKey.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

# Tên bot: bắt buộc khi chạy — python bot.py --name aidancing-vps1
BOT_NAME = None
bot_enabled = False
bot_enabled_lock = threading.Lock()

def is_bot_enabled():
    with bot_enabled_lock:
        return bot_enabled

def set_bot_enabled(value):
    global bot_enabled
    with bot_enabled_lock:
        bot_enabled = bool(value)

# CREATE_URL đã được chuyển thành dynamic theo modelId trong đơn hàng
AIDANCING_ORIGIN = "https://aidancing.net"
DASHBOARD_URL = f"{AIDANCING_ORIGIN}/dashboard"
WORKER_URL = "https://motionai-upload-api.traderfinn0312.workers.dev"
BOT_CHROME_PROFILE = os.path.abspath(os.environ.get("BOT_CHROME_PROFILE", "bot_chrome_profile_web2"))

browser_lock = threading.Lock()
submit_lock = threading.Lock()  # HTTP nạp đơn — tách khỏi poll (browser_lock chỉ Playwright)
_processing_cache_refresh_at = 0.0
_pending_order_queue = []
_pending_queue_lock = threading.Lock()
_pending_worker_started = False
_submitting_orders = set()
_submitting_orders_lock = threading.Lock()
MIN_RENDER_SEC = int(os.environ.get("BOT_MIN_RENDER_SEC", "600"))
VIDEOAIEASY_MIN_RENDER_SEC = int(os.environ.get("VIDEOAIEASY_MIN_RENDER_SEC", "600"))
VIDEOAIEASY_POLL_INTERVAL_SEC = int(os.environ.get("VIDEOAIEASY_POLL_INTERVAL_SEC", "60"))
RENDER_PROVIDER_AIDANCING = "aidancing"
RENDER_PROVIDER_XIAOYANG = "xiaoyang"
RENDER_PROVIDER_VIDEOAIEASY = "videoaieasy"
_RENDER_PROVIDERS = (
    RENDER_PROVIDER_AIDANCING,
    RENDER_PROVIDER_XIAOYANG,
    RENDER_PROVIDER_VIDEOAIEASY,
)

# Aidancing modelId trên web (script.js): fast=124/125, turbo=117
AIDANCING_TURBO_MODEL_IDS = frozenset({"117"})
AIDANCING_FAST_MODEL_IDS = frozenset({"124", "125"})
XIAOYANG_MODAL_STANDARD = "motion_v26"
XIAOYANG_MODAL_TURBO = "motion_v30"

from user_order_notes import (
    USER_NOTE_FILES_INVALID,
    USER_NOTE_FILES_MISSING,
    USER_NOTE_ORDER_FAILED,
    user_note_for_videoaieasy_failure,
)
_active_render_provider = RENDER_PROVIDER_XIAOYANG
_active_render_provider_lock = threading.Lock()
_processing_cache = {}
_processing_cache_lock = threading.Lock()
_xy_http_client = None
_xy_http_client_lock = threading.Lock()
NHAYCLOUD_XY_BOT = "nhaycloud_vps_bot"
XIAOYANG_MAX_CONCURRENT_PER_ACCOUNT = int(get_env("XIAOYANG_MAX_CONCURRENT", "4"))
_xy_web_clients = {}
_xy_web_clients_lock = threading.Lock()
_xy_inflight = {}
_xy_inflight_lock = threading.Lock()
_xy_accounts_cache = None
_xy_accounts_cache_lock = threading.Lock()
VIDEOAIEASY_MAX_CONCURRENT_PER_ACCOUNT = int(get_env("VIDEOAIEASY_MAX_CONCURRENT", "4"))
_vae_web_clients = {}
_vae_web_clients_lock = threading.Lock()
_vae_inflight = {}
_vae_inflight_lock = threading.Lock()
_vae_accounts_cache = None
_vae_accounts_cache_lock = threading.Lock()


def _pop_processing_cache(order_id):
    with _processing_cache_lock:
        _processing_cache.pop(order_id, None)


def _order_already_completed(order_id):
    """Re-fetch Firestore — tránh hoàn đơn / spam Telegram lặp."""
    try:
        snap = db.collection('orders').document(order_id).get()
        if not snap.exists:
            return True
        d = snap.to_dict() or {}
        return d.get('status') == 'completed' or bool(d.get('resultLink'))
    except Exception as e:
        print(f"⚠️ Không đọc được đơn {order_id}: {e}")
        return False


def _skip_if_order_done(order_id, reason):
    if _order_already_completed(order_id):
        print(f"⏭️ Bỏ qua đơn {order_id} — {reason}")
        _pop_processing_cache(order_id)
        return True
    return False


_http_client = None
_http_client_lock = threading.Lock()

_pw_queue = queue.Queue()
_pw_worker_started = False
_pw_worker_lock = threading.Lock()
_pw_worker_tid = None


def _ensure_playwright_worker():
    global _pw_worker_started
    with _pw_worker_lock:
        if _pw_worker_started:
            return
        _pw_worker_started = True
        threading.Thread(
            target=_playwright_worker_loop,
            daemon=True,
            name="playwright-worker",
        ).start()


def _playwright_worker_loop():
    global _pw_worker_tid
    _pw_worker_tid = threading.get_ident()
    while True:
        fn, args, kwargs, done = _pw_queue.get()
        try:
            done["result"] = fn(*args, **kwargs)
        except Exception as e:
            done["error"] = e
        finally:
            done["event"].set()


def run_playwright(fn, *args, **kwargs):
    """Playwright sync API chỉ chạy trên 1 thread — gọi hàm này từ thread khác."""
    if _pw_worker_tid == threading.get_ident():
        return fn(*args, **kwargs)
    _ensure_playwright_worker()
    done = {"event": threading.Event(), "result": None, "error": None}
    _pw_queue.put((fn, args, kwargs, done))
    done["event"].wait()
    if done["error"] is not None:
        raise done["error"]
    return done["result"]


def _get_http_client():
    global _http_client
    with _http_client_lock:
        if _http_client is None:
            _http_client = AidancingApiClient()
        return _http_client


def _reset_http_client():
    global _http_client
    with _http_client_lock:
        _http_client = None


def get_active_render_provider():
    with _active_render_provider_lock:
        return _active_render_provider


def _xiaoyang_modal_for_order(order_data: dict) -> tuple[str, str]:
    """Model thường (fast) → motion v2.6; Turbo (modelId 117) → motion v3.0."""
    model_id = str(order_data.get("modelId") or "").strip()
    if model_id in AIDANCING_TURBO_MODEL_IDS:
        return XIAOYANG_MODAL_TURBO, get_env("XIAOYANG_OPTION_KEY", "default")
    if model_id in AIDANCING_FAST_MODEL_IDS or not model_id:
        return XIAOYANG_MODAL_STANDARD, get_env("XIAOYANG_OPTION_KEY", "default")
    # modelId lạ: ưu tiên env, không thì v2.6
    modal = get_env("XIAOYANG_MODAL_KEY", XIAOYANG_MODAL_STANDARD)
    if modal not in (XIAOYANG_MODAL_STANDARD, XIAOYANG_MODAL_TURBO):
        modal = XIAOYANG_MODAL_STANDARD
    return modal, get_env("XIAOYANG_OPTION_KEY", "default")


def _order_render_provider(order_data: dict) -> str:
    if not order_data:
        return RENDER_PROVIDER_AIDANCING
    rp = (order_data.get("renderProvider") or "").strip().lower()
    if rp in _RENDER_PROVIDERS:
        return rp
    if order_data.get("videoaieasyJobId"):
        return RENDER_PROVIDER_VIDEOAIEASY
    if order_data.get("xiaoyangTaskId"):
        return RENDER_PROVIDER_XIAOYANG
    return RENDER_PROVIDER_AIDANCING


def _use_xiaoyang_web_session() -> bool:
    """Chỉ nhay.cloud bot dùng web session; motionai/app_bot giữ API v1."""
    return bool(BOT_NAME and "nhaycloud" in BOT_NAME.lower())


def _use_videoaieasy() -> bool:
    """Video AI Easy — chỉ nhay.cloud bot."""
    return bool(BOT_NAME and "nhaycloud" in BOT_NAME.lower())


def _videoaieasy_model_for_order(order_data: dict) -> str:
    model_id = str((order_data or {}).get("modelId") or "").strip()
    if model_id in AIDANCING_TURBO_MODEL_IDS:
        return MODEL_KLING_30
    return MODEL_KLING_26


def _xiaoyang_enhance_4k() -> bool:
    return get_env("XIAOYANG_ENHANCE_4K", "1").strip().lower() not in ("0", "false", "no")


def _get_xy_http_client():
    global _xy_http_client
    with _xy_http_client_lock:
        if _xy_http_client is None:
            _xy_http_client = XiaoyangApiClient()
        return _xy_http_client


def _reset_xy_http_client():
    global _xy_http_client
    with _xy_http_client_lock:
        _xy_http_client = None


def _xiaoyang_account_id(email: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (email or "").strip().lower()).strip("_") or "default"


def _load_xiaoyang_accounts():
    """Danh sách nick XiaoYang web: [{id, email, password}, ...]."""
    global _xy_accounts_cache
    with _xy_accounts_cache_lock:
        if _xy_accounts_cache is not None:
            return _xy_accounts_cache
        accounts = []
        raw = (get_env("XIAOYANG_ACCOUNTS") or "").strip()
        if raw:
            if raw.startswith("["):
                import json as _json
                try:
                    for item in _json.loads(raw):
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
        _xy_accounts_cache = accounts
        return accounts


def _xy_inflight_inc(account_id: str):
    with _xy_inflight_lock:
        _xy_inflight[account_id] = _xy_inflight.get(account_id, 0) + 1


def _xy_inflight_dec(account_id: str):
    with _xy_inflight_lock:
        n = _xy_inflight.get(account_id, 0) - 1
        if n <= 0:
            _xy_inflight.pop(account_id, None)
        else:
            _xy_inflight[account_id] = n


def _count_xy_processing_for_account(account_id: str) -> int:
    cache_count = 0
    with _processing_cache_lock:
        for doc in _processing_cache.values():
            d = doc.to_dict() or {}
            if d.get("status") == "processing" and d.get("xiaoyangAccount") == account_id:
                cache_count += 1
    try:
        q = db.collection("orders").where(
            filter=FieldFilter("status", "==", "processing")
        ).where(
            filter=FieldFilter("xiaoyangAccount", "==", account_id)
        )
        db_count = sum(1 for _ in q.stream())
        return max(cache_count, db_count)
    except Exception as e:
        print(f"⚠️ Đếm đơn XiaoYang nick {account_id} (Firestore): {e} — dùng cache={cache_count}")
        return cache_count


def _xy_active_count(account_id: str) -> int:
    with _xy_inflight_lock:
        inflight = _xy_inflight.get(account_id, 0)
    return _count_xy_processing_for_account(account_id) + inflight


def _pick_xiaoyang_account():
    """Chọn nick còn slot (< XIAOYANG_MAX_CONCURRENT_PER_ACCOUNT đơn processing)."""
    accounts = _load_xiaoyang_accounts()
    if not accounts:
        return None
    best = None
    best_count = XIAOYANG_MAX_CONCURRENT_PER_ACCOUNT
    for acc in accounts:
        c = _xy_active_count(acc["id"])
        if c < XIAOYANG_MAX_CONCURRENT_PER_ACCOUNT and c < best_count:
            best = acc
            best_count = c
    return best


def _xiaoyang_account_lookup(account_id: str):
    for acc in _load_xiaoyang_accounts():
        if acc["id"] == account_id:
            return acc
    return None


def _videoaieasy_account_id(email: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (email or "").strip().lower()).strip("_") or "default"


def _load_videoaieasy_accounts():
    global _vae_accounts_cache
    with _vae_accounts_cache_lock:
        if _vae_accounts_cache is not None:
            return _vae_accounts_cache
        accounts = []
        raw = (get_env("VIDEOAIEASY_ACCOUNTS") or "").strip()
        if raw:
            if raw.startswith("["):
                import json as _json
                try:
                    for item in _json.loads(raw):
                        email = (item.get("email") or "").strip()
                        password = item.get("password") or ""
                        if email and password:
                            accounts.append({
                                "id": _videoaieasy_account_id(email),
                                "email": email,
                                "password": password,
                            })
                except Exception as e:
                    print(f"⚠️ VIDEOAIEASY_ACCOUNTS JSON lỗi: {e}")
            else:
                for part in raw.split(","):
                    part = part.strip()
                    if ":" not in part:
                        continue
                    email, password = part.split(":", 1)
                    email, password = email.strip(), password.strip()
                    if email and password:
                        accounts.append({
                            "id": _videoaieasy_account_id(email),
                            "email": email,
                            "password": password,
                        })
        if not accounts:
            email = (get_env("VIDEOAIEASY_EMAIL") or "").strip()
            password = get_env("VIDEOAIEASY_PASSWORD") or ""
            if email and password:
                accounts.append({
                    "id": _videoaieasy_account_id(email),
                    "email": email,
                    "password": password,
                })
        _vae_accounts_cache = accounts
        return accounts


def _vae_inflight_inc(account_id: str):
    with _vae_inflight_lock:
        _vae_inflight[account_id] = _vae_inflight.get(account_id, 0) + 1


def _vae_inflight_dec(account_id: str):
    with _vae_inflight_lock:
        n = _vae_inflight.get(account_id, 0) - 1
        if n <= 0:
            _vae_inflight.pop(account_id, None)
        else:
            _vae_inflight[account_id] = n


def _count_vae_processing_for_account(account_id: str) -> int:
    cache_count = 0
    with _processing_cache_lock:
        for doc in _processing_cache.values():
            d = doc.to_dict() or {}
            if d.get("status") == "processing" and d.get("videoaieasyAccount") == account_id:
                cache_count += 1
    try:
        q = db.collection("orders").where(
            filter=FieldFilter("status", "==", "processing")
        ).where(
            filter=FieldFilter("videoaieasyAccount", "==", account_id)
        )
        db_count = sum(1 for _ in q.stream())
        return max(cache_count, db_count)
    except Exception as e:
        print(f"⚠️ Đếm đơn VideoAiEasy nick {account_id} (Firestore): {e} — dùng cache={cache_count}")
        return cache_count


def _vae_active_count(account_id: str) -> int:
    with _vae_inflight_lock:
        inflight = _vae_inflight.get(account_id, 0)
    return _count_vae_processing_for_account(account_id) + inflight


def _pick_videoaieasy_account():
    accounts = _load_videoaieasy_accounts()
    if not accounts:
        return None
    best = None
    best_count = VIDEOAIEASY_MAX_CONCURRENT_PER_ACCOUNT
    for acc in accounts:
        c = _vae_active_count(acc["id"])
        if c < VIDEOAIEASY_MAX_CONCURRENT_PER_ACCOUNT and c < best_count:
            best = acc
            best_count = c
    return best


def _videoaieasy_account_lookup(account_id: str):
    for acc in _load_videoaieasy_accounts():
        if acc["id"] == account_id:
            return acc
    return None


def _get_vae_web_client(account_id: str) -> VideoAiEasyClient:
    with _vae_web_clients_lock:
        if account_id not in _vae_web_clients:
            _vae_web_clients[account_id] = VideoAiEasyClient(account_id)
        return _vae_web_clients[account_id]


def _reset_vae_web_client(account_id: str | None = None):
    with _vae_web_clients_lock:
        if account_id:
            _vae_web_clients.pop(account_id, None)
        else:
            _vae_web_clients.clear()


def _ensure_vae_web_session(api: VideoAiEasyClient, email: str, password: str):
    return api.ensure_session(email, password)


def _get_xy_web_client(account_id=None):
    key = (account_id or BOT_NAME or NHAYCLOUD_XY_BOT).lower()
    with _xy_web_clients_lock:
        if key not in _xy_web_clients:
            _xy_web_clients[key] = XiaoyangWebClient(account_id=key)
        return _xy_web_clients[key]


def _reset_xy_web_client(account_id=None):
    with _xy_web_clients_lock:
        if account_id:
            _xy_web_clients.pop(account_id.lower(), None)
        else:
            _xy_web_clients.clear()


def _ensure_xy_web_session(client: XiaoyangWebClient, email=None, password=None):
    try:
        return client.me()
    except XiaoyangWebAuthError:
        client.login(email=email, password=password)
        return client.me()


def _order_xiaoyang_submit_mode(order_data: dict) -> str:
    return (order_data or {}).get("xiaoyangSubmitMode") or "api"


def _normalize_render_provider(value, default=RENDER_PROVIDER_XIAOYANG):
    p = (value or default).strip().lower()
    if p not in _RENDER_PROVIDERS:
        return default
    return p


def _apply_render_provider(provider, source=""):
    global _active_render_provider
    provider = _normalize_render_provider(provider)
    with _active_render_provider_lock:
        prev = _active_render_provider
        _active_render_provider = provider
    if provider != prev:
        suffix = f" ({source})" if source else ""
        print(f"\n🔀 Render provider: {prev} → {provider}{suffix} (đơn đang chạy giữ engine cũ)\n")
    return provider


def _render_provider_from_bot_data(data: dict) -> str:
    if not data:
        return RENDER_PROVIDER_XIAOYANG
    return _normalize_render_provider(
        data.get("activeRenderProvider") or data.get("activeProvider")
    )


def start_render_provider_listener():
    """Đọc engine ban đầu từ bots/{BOT_NAME}; đổi realtime qua on_bot_config_snapshot."""

    initial = RENDER_PROVIDER_XIAOYANG
    bot_doc = db.collection("bots").document(BOT_NAME).get()
    if bot_doc.exists:
        initial = _render_provider_from_bot_data(bot_doc.to_dict() or {})
    else:
        legacy = db.collection("settings").document("render").get()
        if legacy.exists:
            initial = _normalize_render_provider(
                (legacy.to_dict() or {}).get("activeProvider")
            )
    _apply_render_provider(initial)
    print(f"🎬 Render provider (đơn mới): {initial}")


def _http_create_job(model_id, char_path, vid_path):
    api = _get_http_client()
    return api.create_job(model_id, char_path, vid_path)


def _http_poll_orders(orders_to_check):
    api = _get_http_client()
    job_ids = [str(doc.to_dict().get('aidancingJobId')) for doc in orders_to_check]
    jobs_map = api.find_jobs_by_ids(job_ids)
    for doc in orders_to_check:
        job_id = str(doc.to_dict().get('aidancingJobId'))
        print(f"🧐 API — Job {job_id}...")
        job = jobs_map.get(int(job_id))
        if not job:
            print(f"❌ Không thấy job {job_id} trong API (3 trang đầu)")
            continue
        status = (job.get('status') or '').upper()
        print(f"   status={status}, outputFileId={job.get('outputFileId')}")
        if status == 'COMPLETED' and job.get('outputFileId'):
            if _skip_if_order_done(doc.id, "đã completed trên Firestore"):
                continue
            print(f"🎉 Job {job_id} HOÀN TẤT — tải file {job['outputFileId']}...")
            try:
                local_vid = api.download_file(job['outputFileId'], f"res_{doc.id}.mp4")
                _complete_order_with_video(doc, local_vid)
            except Exception as e:
                print(f"⚠️ Lỗi tải/hoàn đơn {doc.id}: {e}")
        elif status in ('FAILED', 'ERROR', 'CANCELLED'):
            print(f"❌ Job {job_id} thất bại trên aidancing ({status})")
            order_data = doc.to_dict()
            err_detail = f'Aidancing job {job_id} {status}: {job.get("errorMessage") or ""}'
            notify_internal_error_telegram(doc.id, order_data, err_detail, 'render aidancing')
            cost_coins = order_data.get('costCoins', 0)
            user_id = order_data.get('userId')
            if cost_coins > 0 and user_id:
                try:
                    db.collection('users').document(user_id).update({'coins': firestore.Increment(cost_coins)})
                except Exception as e:
                    print(f"⚠️ Hoàn coin lỗi: {e}")
            db.collection('orders').document(doc.id).update({
                'status': 'failed',
                'adminNote': firestore.DELETE_FIELD,
                'systemNote': USER_NOTE_ORDER_FAILED,
                'updatedAt': firestore.SERVER_TIMESTAMP
            })
            _pop_processing_cache(doc.id)
        else:
            print(f"⏳ Job {job_id} vẫn {status}")


def _fail_order_processing(doc, order_data, err_detail, system_note, context: str):
    notify_internal_error_telegram(doc.id, order_data, err_detail, context)
    cost_coins = order_data.get("costCoins", 0)
    user_id = order_data.get("userId")
    if cost_coins > 0 and user_id:
        try:
            db.collection("users").document(user_id).update({"coins": firestore.Increment(cost_coins)})
        except Exception as e:
            print(f"⚠️ Hoàn coin lỗi: {e}")
    db.collection("orders").document(doc.id).update({
        "status": "failed",
        "adminNote": firestore.DELETE_FIELD,
        "systemNote": system_note,
        "updatedAt": firestore.SERVER_TIMESTAMP,
    })
    _pop_processing_cache(doc.id)


def _http_poll_xiaoyang_orders(orders_to_check):
    for doc in orders_to_check:
        order_data = doc.to_dict() or {}
        task_id = str(order_data.get("xiaoyangTaskId") or "").strip()
        if not task_id:
            continue
        web_mode = _order_xiaoyang_submit_mode(order_data) == "web"
        label = "Web" if web_mode else "API"
        print(f"🧐 XiaoYang {label} — task {task_id} (đơn {doc.id})...")
        try:
            if web_mode:
                account_id = (order_data.get("xiaoyangAccount") or NHAYCLOUD_XY_BOT).strip()
                acc = _xiaoyang_account_lookup(account_id)
                api = _get_xy_web_client(account_id)
                if acc:
                    _ensure_xy_web_session(api, acc["email"], acc["password"])
                else:
                    _ensure_xy_web_session(api)
                t = api.get_task(task_id)
            else:
                api = _get_xy_http_client()
                t = api.get_task(task_id)
        except (XiaoyangApiAuthError, XiaoyangApiError, XiaoyangWebAuthError, XiaoyangWebError) as e:
            print(f"❌ Poll XiaoYang {task_id}: {e}")
            if web_mode:
                if isinstance(e, XiaoyangWebAuthError):
                    account_id = (order_data.get("xiaoyangAccount") or "").strip()
                    _reset_xy_web_client(account_id or None)
            elif "401" in str(e) or "403" in str(e):
                _reset_xy_http_client()
            continue
        st = (t.get("status") or "").upper()
        err = t.get("error_message")
        stage = ""
        if t.get("enhance_4k") and t.get("enhance_stage") == "enhancing" and st != "SUCCESS":
            stage = " (HD 2K)"
        print(f"   status={st}{stage}" + (f" — {err}" if err else ""))
        if st == "SUCCESS":
            if _skip_if_order_done(doc.id, "đã completed trên Firestore"):
                continue
            print(f"🎉 XiaoYang task {task_id} HOÀN TẤT — tải video...")
            try:
                if web_mode:
                    local_vid = api.download_task_file(task_id, f"res_{doc.id}.mp4")
                    _complete_order_with_video(doc, local_vid)
                else:
                    local_vid = api.download_task(task_id, f"res_{doc.id}.mp4")
                    if _complete_order_with_video(doc, local_vid):
                        api.try_delete_task(task_id)
            except Exception as e:
                print(f"⚠️ Lỗi tải/hoàn đơn {doc.id}: {e}")
        elif st == "FAIL":
            print(f"❌ Task {task_id} FAIL trên XiaoYang")
            _fail_order_processing(
                doc,
                order_data,
                f"XiaoYang task {task_id} FAIL: {err or ''}",
                USER_NOTE_ORDER_FAILED,
                "render xiaoyang",
            )
            if not web_mode:
                try:
                    api.try_delete_task(task_id)
                except Exception:
                    pass
        else:
            print(f"⏳ Task {task_id} vẫn {st} (QUEUED/PENDING/PROCESSING)")


def _http_poll_videoaieasy_orders(orders_to_check):
    for doc in orders_to_check:
        order_data = doc.to_dict() or {}
        job_id = str(order_data.get("videoaieasyJobId") or "").strip()
        if not job_id:
            continue
        account_id = (order_data.get("videoaieasyAccount") or "").strip()
        nick = order_data.get("videoaieasyAccountEmail") or account_id
        print(f"🧐 VideoAiEasy — job {job_id} (đơn {doc.id}, {nick})...")
        job = None
        api = None
        try:
            api = _get_vae_web_client(account_id or "default")
            acc = _videoaieasy_account_lookup(account_id)
            if acc:
                _ensure_vae_web_session(api, acc["email"], acc["password"])
            job = api.get_job(job_id)
        except VideoAiEasyAuthError as e:
            print(f"❌ Poll VideoAiEasy {job_id}: {e}")
            if account_id:
                _reset_vae_web_client(account_id)
            acc = _videoaieasy_account_lookup(account_id)
            if acc:
                try:
                    api = _get_vae_web_client(account_id)
                    _ensure_vae_web_session(api, acc["email"], acc["password"])
                    job = api.get_job(job_id)
                    print(f"🔑 Poll VideoAiEasy {job_id} OK sau login lại ({nick})")
                except (VideoAiEasyAuthError, VideoAiEasyError) as e2:
                    print(f"❌ Poll VideoAiEasy {job_id} vẫn lỗi sau login: {e2}")
                    continue
            else:
                continue
        except VideoAiEasyError as e:
            print(f"❌ Poll VideoAiEasy {job_id}: {e}")
            if api and ("500" in str(e) or "404" in str(e)):
                try:
                    print(f"↪️ Thử download trực tiếp job {job_id}...")
                    local_vid = api.download_job(job_id, f"res_{doc.id}.mp4")
                    _complete_order_with_video(doc, local_vid)
                    continue
                except Exception as dl_err:
                    print(f"⚠️ Download trực tiếp {job_id} thất bại: {dl_err}")
            continue
        except Exception as e:
            err = str(e)
            if "padding" in err.lower() or "cookie" in err.lower():
                print(f"❌ Poll VideoAiEasy {job_id}: session lỗi ({e})")
                if account_id:
                    _reset_vae_web_client(account_id)
                acc = _videoaieasy_account_lookup(account_id)
                if acc:
                    try:
                        api = _get_vae_web_client(account_id)
                        _ensure_vae_web_session(api, acc["email"], acc["password"])
                        job = api.get_job(job_id)
                        print(f"🔑 Poll VideoAiEasy {job_id} OK sau login lại ({nick})")
                    except Exception as e2:
                        print(f"❌ Poll VideoAiEasy {job_id} vẫn lỗi sau login: {e2}")
                continue
            print(f"❌ Poll VideoAiEasy {job_id}: {e}")
            continue
        if job is None:
            continue
        status = (job.get("status") or "").lower()
        err = job.get("error_message")
        print(f"   status={status}" + (f" — {err}" if err else ""))
        if status == "done":
            if _skip_if_order_done(doc.id, "đã completed trên Firestore"):
                continue
            print(f"🎉 VideoAiEasy job {job_id} HOÀN TẤT — tải video...")
            try:
                local_vid = api.download_job(job_id, f"res_{doc.id}.mp4")
                _complete_order_with_video(doc, local_vid)
            except Exception as e:
                print(f"⚠️ Lỗi tải/hoàn đơn {doc.id}: {e}")
        elif status in ("failed", "expired"):
            print(f"❌ Job {job_id} {status} trên VideoAiEasy")
            _fail_order_processing(
                doc,
                order_data,
                f"VideoAiEasy job {job_id} {status}: {err or ''}",
                user_note_for_videoaieasy_failure(err),
                "render videoaieasy",
            )
        else:
            print(f"⏳ Job {job_id} vẫn {status}")


def _min_render_sec_for_order(order_data: dict) -> int:
    if _order_render_provider(order_data) == RENDER_PROVIDER_VIDEOAIEASY:
        return VIDEOAIEASY_MIN_RENDER_SEC
    return MIN_RENDER_SEC


def _processing_monitor_state():
    """Đọc từ RAM — poll sau min_render theo từng engine (VAE: 10p mặc định)."""
    now = datetime.now(timezone.utc)
    ad_eligible = []
    xy_eligible = []
    vae_eligible = []
    vae_processing_count = 0
    with _processing_cache_lock:
        stale_ids = []
        for oid, doc in _processing_cache.items():
            d = doc.to_dict() or {}
            if d.get("status") != "processing":
                stale_ids.append(oid)
        for oid in stale_ids:
            _processing_cache.pop(oid, None)
        processing_count = len(_processing_cache)
        for doc in _processing_cache.values():
            d = doc.to_dict() or {}
            if d.get("status") != "processing":
                continue
            rp = _order_render_provider(d)
            if rp == RENDER_PROVIDER_VIDEOAIEASY and d.get("videoaieasyJobId"):
                vae_processing_count += 1
            submitted_at = d.get("submittedAt")
            min_render_sec = _min_render_sec_for_order(d)
            if submitted_at and (now - submitted_at).total_seconds() <= min_render_sec:
                continue
            if rp == RENDER_PROVIDER_XIAOYANG:
                if d.get("xiaoyangTaskId"):
                    xy_eligible.append(doc)
            elif rp == RENDER_PROVIDER_VIDEOAIEASY:
                if d.get("videoaieasyJobId"):
                    vae_eligible.append(doc)
            else:
                job_id = d.get("aidancingJobId")
                if job_id and job_id != "MANUAL":
                    ad_eligible.append(doc)
    return ad_eligible, xy_eligible, vae_eligible, processing_count, vae_processing_count


def on_processing_orders_snapshot(snapshot, changes, read_time):
    """Đồng bộ cache từ snapshot đầy đủ — tránh mất đơn khi listener reconnect (GOAWAY)."""
    with _processing_cache_lock:
        fresh = {}
        for doc in snapshot:
            d = doc.to_dict() or {}
            if d.get("status") == "processing":
                fresh[doc.id] = doc
        _processing_cache.clear()
        _processing_cache.update(fresh)


def _refresh_processing_cache_from_firestore():
    fresh = {
        doc.id: doc
        for doc in db.collection("orders")
        .where(filter=FieldFilter("status", "==", "processing"))
        .stream()
    }
    with _processing_cache_lock:
        _processing_cache.clear()
        _processing_cache.update(fresh)
    print(f"🔄 Refresh processing cache: {len(fresh)} đơn")
    return len(fresh)


def start_processing_listener():
    db.collection('orders').where(
        filter=FieldFilter("status", "==", "processing")
    ).on_snapshot(on_processing_orders_snapshot)
    print("👂 Listener processing orders — cache RAM, không query Firestore mỗi lần poll")
    try:
        _refresh_processing_cache_from_firestore()
    except Exception as e:
        print(f"⚠️ Nạp processing cache lúc khởi động: {e}")


def _submit_engine_lock():
    """HTTP: submit_lock — poll monitor chạy song song. Browser: browser_lock (Playwright)."""
    return submit_lock if use_api_mode() else browser_lock


def _maybe_refresh_processing_cache():
    """Phòng listener Firestore GOAWAY — đồng bộ lại cache processing định kỳ."""
    global _processing_cache_refresh_at
    if not use_api_mode():
        return
    interval = int(os.environ.get("BOT_PROCESSING_CACHE_REFRESH_SEC", "120"))
    now = time.time()
    if now - _processing_cache_refresh_at < interval:
        return
    _processing_cache_refresh_at = now
    try:
        _refresh_processing_cache_from_firestore()
    except Exception as e:
        print(f"⚠️ Refresh processing cache: {e}")


def _monitor_sleep_seconds(eligible_count, processing_count, *, vae_processing_count=0):
    """Không có webhook aidancing — chỉ poll; VAE: 1p/lần khi có đơn VAE đang chạy."""
    idle = int(os.environ.get("BOT_POLL_IDLE_SEC", "300"))
    wait_render = int(os.environ.get("BOT_POLL_WAIT_RENDER_SEC", "120"))
    active = int(os.environ.get("BOT_POLL_ACTIVE_SEC", "90"))
    if processing_count == 0:
        return idle
    if vae_processing_count > 0:
        return VIDEOAIEASY_POLL_INTERVAL_SEC
    if eligible_count == 0:
        return wait_render
    return active


def ensure_cdp_available(cdp_url, timeout=3):
    try:
        url = cdp_url.rstrip("/") + "/json/version"
        requests.get(url, timeout=timeout)
        return True
    except Exception:
        return False

def _cdp_not_running_error(cdp_url):
    return RuntimeError(
        f"Chrome CDP chưa chạy tại {cdp_url}. "
        "Mở Chrome ở terminal RIÊNG và GIỮ chạy (đừng Ctrl+C), rồi chạy bot:\n"
        "  /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome \\\n"
        "    --remote-debugging-port=9223 --remote-allow-origins='*' \\\n"
        "    --user-data-dir=\"$HOME/.chrome-aidancing-wallpaper\" \\\n"
        "    --profile-directory=\"Profile 14\""
    )

def _ensure_pending_worker():
    global _pending_worker_started
    with _pending_queue_lock:
        if _pending_worker_started:
            return
        _pending_worker_started = True
        threading.Thread(target=_pending_order_worker, daemon=True).start()

def _pending_order_worker():
    while True:
        order_id = None
        with _pending_queue_lock:
            if _pending_order_queue:
                order_id = _pending_order_queue.pop(0)
        if order_id:
            try:
                submit_order(order_id)
            except Exception as e:
                print(f"❌ Lỗi nạp đơn {order_id}: {e}")
                _session_error_backoff[order_id] = time.time() + SESSION_ERROR_BACKOFF_SEC
        else:
            time.sleep(0.5)

AIDANCING_BLOCKED_MARKERS = (
    "bảo trì", "bao tri", "maintenance", "under maintenance",
    "scheduled maintenance", "hệ thống đang", "temporarily unavailable",
    "service unavailable", "coming soon",
)

STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {}, loadTimes: function() {}, csi: function() {} };
Object.defineProperty(navigator, 'languages', { get: () => ['vi-VN', 'vi', 'en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) =>
  parameters.name === 'notifications'
    ? Promise.resolve({ state: Notification.permission })
    : originalQuery(parameters);
"""

class AidancingBrowserSession:
    """Wrapper: CDP mode không đóng Chrome của user khi bot xong."""

    def __init__(self, context, close_context_on_exit=True):
        self.context = context
        self.close_context_on_exit = close_context_on_exit
        self._pages = []

    def new_page(self):
        page = self.context.new_page()
        self._pages.append(page)
        return page

    def cookies(self, urls=None):
        if urls:
            return self.context.cookies(urls)
        return self.context.cookies()

    def clear_cookies(self):
        self.context.clear_cookies()

    def close(self):
        for page in self._pages:
            try:
                page.close()
            except Exception:
                pass
        self._pages.clear()
        if self.close_context_on_exit:
            try:
                self.context.close()
            except Exception:
                pass

def close_extra_aidancing_tabs(session, keep_page):
    """Đóng tab aidancing phụ (do nút Tải mở target=_blank)."""
    for p in list(session.context.pages):
        if p == keep_page:
            continue
        try:
            u = p.url or ''
            if 'aidancing' in u or u.startswith('blob:') or 'proxy/files' in u:
                p.close()
        except Exception:
            pass

def _apply_stealth(context):
    try:
        context.add_init_script(STEALTH_INIT_SCRIPT)
    except Exception as e:
        print(f"⚠️ Không gắn stealth script: {e}")

def _aidancing_chrome_args():
    args = [
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if os.environ.get("BOT_CHROME_OFFSCREEN", "0") == "1":
        args.append("--window-position=-2400,-2400")
    return args

def _chrome_profile_dir():
    return os.path.abspath(os.environ.get("BOT_CHROME_PROFILE", BOT_CHROME_PROFILE))

def launch_aidancing_browser(playwright):
    cdp_url = os.environ.get("BOT_CDP_URL", "").strip()
    if cdp_url:
        if not ensure_cdp_available(cdp_url):
            raise _cdp_not_running_error(cdp_url)
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context(
            locale="vi-VN",
            timezone_id="Asia/Ho_Chi_Minh",
            viewport={"width": 1280, "height": 800},
        )
        _apply_stealth(context)
        print(f"🔗 Nối Chrome qua CDP ({cdp_url}) — dùng Chrome thật, không đóng khi bot xong.")
        return AidancingBrowserSession(context, close_context_on_exit=False)

    profile_dir = _chrome_profile_dir()
    kwargs = dict(
        user_data_dir=profile_dir,
        headless=False,
        slow_mo=int(os.environ.get("BOT_SLOW_MO", "500")),
        ignore_default_args=["--enable-automation"],
        args=_aidancing_chrome_args(),
        viewport={"width": 1280, "height": 800},
        locale="vi-VN",
        timezone_id="Asia/Ho_Chi_Minh",
    )
    try:
        context = playwright.chromium.launch_persistent_context(channel="chrome", **kwargs)
    except Exception as e:
        print(f"⚠️ Không mở được Chrome ({e}), dùng Chromium bundled...")
        context = playwright.chromium.launch_persistent_context(**kwargs)
    _apply_stealth(context)
    return AidancingBrowserSession(context, close_context_on_exit=True)

def _aidancing_page_info(page):
    try:
        return f"{page.url} | {page.title()}"
    except Exception:
        return page.url

def is_aidancing_blocked(page):
    try:
        url = (page.url or "").lower()
        if any(x in url for x in ("maintenance", "maintain", "bao-tri")):
            return True
        combined = f"{page.title() or ''} {page.content()}".lower()
        return any(marker in combined for marker in AIDANCING_BLOCKED_MARKERS)
    except Exception:
        return False

def _raise_if_aidancing_blocked(page):
    if not is_aidancing_blocked(page):
        return
    print(f"🚫 Aidancing chặn/trang bảo trì: {_aidancing_page_info(page)}")
    raise RuntimeError(
        "Aidancing hiển thị trang bảo trì hoặc chặn trình duyệt tự động. "
        "Thường do profile Chrome BOT chưa có cookie đăng nhập (Chrome thường của bạn vẫn vào được vì đã login). "
        "Cách xử lý: thoát hết Chrome (Cmd+Q), copy profile Default đã login sang ~/.chrome-aidancing-bot "
        "(xem README hoặc hướng dẫn setup), mở Chrome CDP rồi BOT_CDP_URL=http://127.0.0.1:9223 python3 bot.py --name wallpaper --mode api"
    )

def _aidancing_on_dashboard(page):
    u = page.url.lower()
    if "login" in u or "signin" in u or "sign-in" in u:
        return False
    if is_aidancing_blocked(page):
        return False
    return "dashboard" in u

def goto_aidancing_dashboard(page, session, login_wait_sec=120):
    """Mở dashboard; xử lý redirect loop (cookie hỏng) và chờ đăng nhập thủ công."""

    def _goto(url):
        page.goto(url, timeout=60000, wait_until="domcontentloaded")
        print(f"📄 {_aidancing_page_info(page)}")
        _raise_if_aidancing_blocked(page)

    try:
        _goto(DASHBOARD_URL)
    except Exception as e:
        err = str(e)
        if "Aidancing hiển thị" in err:
            raise
        if "ERR_TOO_MANY_REDIRECTS" in err or "too many redirects" in err.lower():
            print("⚠️ Redirect loop — xóa cookie profile bot và thử lại...")
            try:
                session.clear_cookies()
            except Exception as ce:
                print(f"   (không xóa được cookie: {ce})")
            _goto(AIDANCING_ORIGIN)
            page.wait_for_timeout(2000)
            _goto(DASHBOARD_URL)
        else:
            raise

    page.wait_for_timeout(2000)
    if _aidancing_on_dashboard(page):
        return

    print(f"⚠️ Chưa vào Dashboard (URL: {page.url})")
    print("👉 Đăng nhập aidancing.net trên cửa sổ Chrome BOT (thư mục bot_chrome_profile).")
    print("   Chrome thường của bạn dùng profile khác — cần login 1 lần trên cửa sổ bot.")

    deadline = time.time() + login_wait_sec
    while time.time() < deadline:
        page.wait_for_timeout(3000)
        if _aidancing_on_dashboard(page):
            print("✅ Đã vào Dashboard sau khi đăng nhập.")
            return
        try:
            _goto(DASHBOARD_URL)
        except Exception as e:
            if "Aidancing hiển thị" in str(e):
                raise

    raise RuntimeError(
        f"Không vào được Dashboard sau {login_wait_sec}s. "
        f"Đăng nhập trên cửa sổ Chrome bot rồi chạy lại. URL: {page.url}"
    )

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8855918099:AAHmPUWTe6_dicXyh0nseADQomVv6MGKjGQ")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "6067707939")
AIDANCING_LOW_BALANCE_THRESHOLD = 10

def normalize_bot_name(name):
    name = (name or '').strip().lower()
    name = re.sub(r'[^a-z0-9_-]', '-', name)
    name = re.sub(r'-+', '-', name).strip('-')
    return name[:64]

def ensure_bot_registered():
    ref = db.collection('bots').document(BOT_NAME)
    doc = ref.get()
    now = firestore.SERVER_TIMESTAMP
    if not doc.exists:
        ref.set({
            'name': BOT_NAME,
            'displayName': BOT_NAME,
            'enabled': False,
            'hostname': socket.gethostname(),
            'createdAt': now,
            'startedAt': now,
        })
        print(f"🆕 Bot mới đăng ký trên Firestore: {BOT_NAME} (mặc định TẮT — bật trên Admin)")
    else:
        ref.set({
            'name': BOT_NAME,
            'startedAt': now,
            'hostname': socket.gethostname(),
        }, merge=True)

def on_bot_config_snapshot(keys, changes, read_time):
    # Document watch callback: (sorted_keys, DocumentChange[], read_time) — not a DocumentSnapshot.
    if not changes:
        return
    enabled = False
    data = {}
    for change in changes:
        doc = change.document
        if getattr(doc, 'exists', False):
            data = doc.to_dict() or {}
            enabled = bool(data.get('enabled', False))
        break
    prev = is_bot_enabled()
    set_bot_enabled(enabled)
    if enabled != prev:
        status = "🟢 BẬT — bot đang xử lý đơn" if enabled else "🔴 TẮT — bot không làm gì"
        print(f"\n[{BOT_NAME}] Admin đổi trạng thái: {status}\n")
    if data:
        _apply_render_provider(_render_provider_from_bot_data(data), source="admin")

def start_bot_control_listener():
    ensure_bot_registered()
    doc = db.collection('bots').document(BOT_NAME).get()
    set_bot_enabled(bool(doc.to_dict().get('enabled', False)) if doc.exists else False)
    status = "🟢 BẬT" if is_bot_enabled() else "🔴 TẮT"
    print(f"[{BOT_NAME}] Trạng thái hiện tại: {status}")
    if not is_bot_enabled():
        print("⏸️  Bot đang TẮT. Vào Admin → Bots để bật.")

    db.collection('bots').document(BOT_NAME).on_snapshot(on_bot_config_snapshot)

def send_telegram_message(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML"
        }
        res = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=10)
        if res.status_code != 200:
            print(f"❌ Lỗi gửi tin nhắn Telegram: {res.status_code} - {res.text}")
    except Exception as e:
        print(f"❌ Lỗi kết nối gửi Telegram: {e}")

_INTERNAL_ERROR_MARKERS = (
    'aidancing', 'xiaoyang', 'xiao yang', '/api/proxy/', 'proxy/jobs', 'proxy/files',
    '401', '503', '502', '429', '400', '504', 'đăng nhập lại', 'bảo trì',
    'chrome cdp', 'connect_over_cdp', 'econnrefused', 'target closed',
    'different thread', 'job id', 'dashboard', 'create/general',
    'bot nạp', 'maintenance', 'option_key', 'modal_key', 'direct_media',
    'workers', 'e_direct_media', 'session expired', 'cookie',
)
_ERROR_TELEGRAM_COOLDOWN = 900
_error_telegram_sent = {}
_error_telegram_lock = threading.Lock()
_session_error_backoff = {}
SESSION_ERROR_BACKOFF_SEC = 300

def is_internal_bot_error(err):
    s = (err or '').lower()
    return any(m in s for m in _INTERNAL_ERROR_MARKERS)

def notify_internal_error_telegram(order_id, order_data, err, context=''):
    now = time.time()
    with _error_telegram_lock:
        last = _error_telegram_sent.get(order_id, 0)
        if now - last < _ERROR_TELEGRAM_COOLDOWN:
            return
        _error_telegram_sent[order_id] = now
    short_id = order_id[-6:].upper()
    user_name = (order_data or {}).get('userName', 'Khách hàng')
    user_email = (order_data or {}).get('userEmail', 'N/A')
    ctx = f" ({context})" if context else ""
    err_text = (err or '')[:500]
    msg = (
        f"🚨 <b>BOT LỖI NỘI BỘ{ctx}</b>\n\n"
        f"🆔 Mã đơn: #{short_id}\n"
        f"👤 Khách: {user_name}\n"
        f"📧 Email: {user_email}\n"
        f"⚠️ Chi tiết:\n<code>{err_text}</code>"
    )
    send_telegram_message(msg)

def apply_bot_error_update(doc_ref, order_id, order_data, err, context='nạp đơn'):
    """Lỗi hạ tầng → Telegram admin; không ghi chi tiết kỹ thuật ra đơn cho khách đọc."""
    notify_internal_error_telegram(order_id, order_data, err, context)
    _session_error_backoff[order_id] = time.time() + SESSION_ERROR_BACKOFF_SEC
    return True

def _pending_submit_backoff_active(order_id):
    return time.time() < _session_error_backoff.get(order_id, 0)

def scrape_aidancing_balance(page):
    """Đọc số coin còn lại trên header aidancing.net (vd: 101.0)."""
    try:
        val = page.evaluate('''() => {
            const pick = (s) => {
                const m = String(s).trim().match(/^(\\d+(?:\\.\\d+)?)$/);
                return m ? parseFloat(m[1]) : null;
            };
            const scopes = document.querySelectorAll('header *, nav *, [class*="wallet"], [class*="balance"], [class*="coin"]');
            for (const el of scopes) {
                if (el.children.length > 0) continue;
                const v = pick(el.textContent);
                if (v !== null && v >= 0 && v < 100000) return v;
            }
            return null;
        }''')
        if val is not None:
            return float(val)
    except Exception as e:
        print(f"⚠️ Không đọc được balance aidancing: {e}")
    return None

def alert_low_aidancing_balance(balance, extra=''):
    if balance is None or balance >= AIDANCING_LOW_BALANCE_THRESHOLD:
        return
    msg = (
        f"🚨🚨 <b>CẢNH BÁO KHẨN — SẮP HẾT COIN AIDANCING!</b>\n\n"
        f"💰 Số dư aidancing.net: <b>{balance}</b> Coin\n"
        f"⚠️ Dưới ngưỡng {AIDANCING_LOW_BALANCE_THRESHOLD} Coin — "
        f"<b>nạp gấp</b> trước khi bot không tạo được đơn!\n"
        f"{extra}"
    )
    send_telegram_message(msg)

def download_file(url, filename, cookies=None, referer=None, retries=2):
    print(f"📥 Tải file (requests): {filename}...")
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': referer or f'{AIDANCING_ORIGIN}/dashboard',
        'Origin': AIDANCING_ORIGIN,
    }
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, headers=headers, cookies=cookies, timeout=120)
            if response.status_code in (503, 502, 429) and attempt < retries:
                wait = 5 * attempt
                print(f"⚠️ HTTP {response.status_code} — thử lại {attempt}/{retries} sau {wait}s...")
                time.sleep(wait)
                continue
            response.raise_for_status()
            with open(filename, 'wb') as f:
                f.write(response.content)
            return os.path.abspath(filename)
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(3 * attempt)
    print(f"❌ Lỗi tải file: {last_err}")
    return None

def download_aidancing_result(session, page, url, filename, download_locator=None):
    """Tải video kết quả aidancing — không click mở tab (aidancing dùng target=_blank)."""
    print(f"📥 Tải kết quả aidancing: {filename}...")
    if not url.startswith('http'):
        url = AIDANCING_ORIGIN + url

    def save_bytes(data):
        with open(filename, 'wb') as f:
            f.write(data)
        return os.path.abspath(filename)

    def session_get(target_url, label):
        try:
            resp = session.context.request.get(
                target_url,
                headers={'Referer': DASHBOARD_URL, 'Origin': AIDANCING_ORIGIN},
                timeout=120000,
            )
            if resp.ok:
                save_bytes(resp.body())
                print(f"✅ {label}")
                return os.path.abspath(filename)
            print(f"⚠️ {label} — HTTP {resp.status}")
        except Exception as e:
            print(f"⚠️ {label} — {e}")
        return None

    # 1) Tải thẳng URL proxy/API — không click (tránh mở tab mới)
    result = session_get(url, "Tải direct URL (session cookie)")
    if result:
        return result

    # 2) fetch() ngay trên dashboard (credentials: include)
    try:
        data = page.evaluate('''async (videoUrl) => {
            const r = await fetch(videoUrl, { credentials: 'include' });
            if (!r.ok) return { ok: false, status: r.status };
            const buf = await r.arrayBuffer();
            const bytes = new Uint8Array(buf);
            let binary = '';
            const chunk = 0x8000;
            for (let i = 0; i < bytes.length; i += chunk) {
                binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
            }
            return { ok: true, b64: btoa(binary) };
        }''', url)
        if data and data.get('ok') and data.get('b64'):
            save_bytes(base64.b64decode(data['b64']))
            print("✅ Tải qua fetch in-page")
            return os.path.abspath(filename)
        if data:
            print(f"⚠️ In-page fetch HTTP {data.get('status')}")
    except Exception as e:
        print(f"⚠️ In-page fetch lỗi: {e}")

    # 3) Nút Tải mở tab video mới (target=_blank) — bắt tab, lấy src, đóng tab
    if download_locator is not None and download_locator.count() > 0:
        new_page = None
        try:
            print("🖱️ Nút Tải mở tab mới — bắt tab video...")
            with session.context.expect_page(timeout=30000) as page_info:
                download_locator.click()
            new_page = page_info.value
            new_page.wait_for_load_state('domcontentloaded', timeout=30000)
            new_page.wait_for_timeout(1500)
            video_url = new_page.evaluate('''() => {
                const v = document.querySelector('video');
                if (v) {
                    const s = v.querySelector('source');
                    const src = (s && s.src) || v.src || v.currentSrc || '';
                    if (src) return src;
                }
                return location.href;
            }''')
            if video_url and not video_url.startswith('http'):
                video_url = AIDANCING_ORIGIN + video_url
            if video_url:
                print(f"🔗 URL tab video: {video_url[:100]}...")
                result = session_get(video_url, "Tải từ tab video")
                if result:
                    return result
                result = session_get(url, "Tải lại URL gốc sau tab")
                if result:
                    return result
        except Exception as e:
            print(f"⚠️ Xử lý tab video: {e}")
        finally:
            if new_page:
                try:
                    new_page.close()
                except Exception:
                    pass
            close_extra_aidancing_tabs(session, page)

    # 4) Fallback requests + cookie
    try:
        cookie_list = session.cookies(urls=[AIDANCING_ORIGIN, f"{AIDANCING_ORIGIN}/"])
        jar = {c['name']: c['value'] for c in cookie_list}
    except Exception:
        jar = {c['name']: c['value'] for c in session.cookies()}
    return download_file(url, filename, cookies=jar, referer=DASHBOARD_URL, retries=3)

def upload_to_r2(file_path, folder="results"):
    try:
        return upload_result_file(file_path, folder=folder)
    except DirectMediaError as e:
        print(f"❌ Lỗi R2: {e}")
    except Exception as e:
        print(f"❌ Lỗi R2: {e}")
    return None

def send_completion_email(order_id, order_data, result_link):
    user_email = order_data.get('userEmail')
    user_name = order_data.get('userName', 'Khách hàng')
    service_type = order_data.get('serviceType', 'copy-motion-photo')
    
    if not user_email:
        print("⚠️ Không tìm thấy Email của khách để gửi thông báo hoàn thành đơn.")
        return
        
    print(f"📧 Đang gửi email thông báo hoàn thành đơn tới: {user_email}...")
    
    # Ánh xạ tên dịch vụ tiếng Việt
    service_label = service_type
    if service_type == 'copy-motion-photo':
        service_label = "AI Copy Chuyển Động Vào Ảnh (20s)"
    elif service_type == 'copy-motion-multi':
        service_label = "AI Copy Nhảy Nhiều Người"
    elif service_type == 'char-to-video-fashion':
        service_label = "AI Copy Thời Trang"
    elif service_type == 'char-to-video-ads':
        service_label = "AI Copy Sản Phẩm"

    short_order_id = order_id[-6:].upper()
    
    payload = {
        "service_id": "service_6r6rd2q",
        "template_id": "template_09eir3r",
        "user_id": "92pP97oTzMGR4p_Zp",
        "template_params": {
            "user_name": user_name,
            "user_email": user_email,
            "order_id": short_order_id,
            "result_link": result_link,
            "service_label": service_label
        }
    }
    
    try:
        url = "https://api.emailjs.com/api/v1.0/email/send"
        response = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=15)
        if response.status_code == 200 or response.text == "OK":
            print(f"✅ Gửi email thông báo qua EmailJS thành công!")
        else:
            print(f"❌ Lỗi gửi email qua EmailJS: {response.status_code} - {response.text}")
    except Exception as e:
        print(f"❌ Lỗi kết nối khi gửi email thông báo qua EmailJS: {e}")

def use_api_mode():
    """Pure HTTP — không cần Chrome/Playwright (cookie AIDANCING_COOKIE)."""
    return os.environ.get("BOT_MODE", "browser").strip().lower() in ("api", "http")

def _complete_order_with_video(doc, local_vid):
    """Upload R2 + cập nhật Firestore + thông báo."""
    order_id = doc.id
    if _order_already_completed(order_id):
        print(f"⏭️ Đơn {order_id} đã completed — không gửi lại Telegram/R2")
        _pop_processing_cache(order_id)
        if os.path.exists(local_vid):
            os.remove(local_vid)
        return True
    r2_url = upload_to_r2(local_vid)
    if not r2_url:
        return False
    db.collection('orders').document(doc.id).update({
        'status': 'completed',
        'resultLink': r2_url,
        'updatedAt': firestore.SERVER_TIMESTAMP
    })
    print(f"✅ ĐÃ TRẢ HÀNG CHO ĐƠN {doc.id}")
    try:
        order_data = doc.to_dict()
        short_id = doc.id[-6:].upper()
        user_name = order_data.get('userName', 'Khách hàng')
        user_email = order_data.get('userEmail', 'N/A')
        char_img = order_data.get('characterImageLink', '')
        msg = (
            f"✅ <b>ĐƠN HÀNG HOÀN THÀNH</b>\n\n"
            f"🆔 Mã đơn: #{short_id}\n"
            f"👤 Khách: {user_name}\n"
            f"📧 Email: {user_email}\n"
        )
        if char_img:
            msg += f"📸 Ảnh đầu vào: <a href=\"{char_img}\">Xem ảnh gốc</a>\n"
        msg += f"📹 Kết quả: <a href=\"{r2_url}\">Xem video kết quả</a>"
        send_telegram_message(msg)
    except Exception as tele_err:
        print(f"⚠️ Lỗi gửi thông báo Telegram hoàn thành: {tele_err}")
    try:
        send_completion_email(doc.id, doc.to_dict(), r2_url)
    except Exception as mail_err:
        print(f"⚠️ Không gửi được email thông báo: {mail_err}")
    if os.path.exists(local_vid):
        os.remove(local_vid)
    _pop_processing_cache(order_id)
    return True

def check_finished_orders_api():
    """Monitor Aidancing + XiaoYang + VideoAiEasy — Pure HTTP (không giữ browser_lock)."""
    if not is_bot_enabled():
        return
    _maybe_refresh_processing_cache()
    ad_orders, xy_orders, vae_orders, _, _ = _processing_monitor_state()
    if not ad_orders and not xy_orders and not vae_orders:
        return

    print(
        f"\n🔍 [MONITOR/HTTP] Poll Aidancing={len(ad_orders)} XiaoYang={len(xy_orders)} "
        f"VideoAiEasy={len(vae_orders)} "
        f"(VAE: sau {VIDEOAIEASY_MIN_RENDER_SEC // 60}p, mỗi {VIDEOAIEASY_POLL_INTERVAL_SEC}s; "
        f"khác: sau {MIN_RENDER_SEC // 60}p)..."
    )
    if ad_orders:
        try:
            _http_poll_orders(ad_orders)
        except SessionExpiredError as e:
            print(f"❌ Session hết hạn: {e}")
            _reset_http_client()
        except Exception as e:
            err = str(e)
            print(f"❌ Lỗi monitor Aidancing HTTP: {e}")
            if any(x in err.lower() for x in ("401", "403", "session expired", "aidancing_cookie")):
                _reset_http_client()
    if xy_orders:
        try:
            _http_poll_xiaoyang_orders(xy_orders)
        except Exception as e:
            print(f"❌ Lỗi monitor XiaoYang HTTP: {e}")
    if vae_orders:
        try:
            _http_poll_videoaieasy_orders(vae_orders)
        except Exception as e:
            print(f"❌ Lỗi monitor VideoAiEasy HTTP: {e}")

def _mark_order_processing(
    doc_ref,
    job_id,
    *,
    provider=RENDER_PROVIDER_AIDANCING,
    xiaoyang_mode=None,
    xiaoyang_account=None,
    xiaoyang_account_email=None,
    videoaieasy_account=None,
    videoaieasy_account_email=None,
):
    """Chỉ chuyển processing sau khi engine render đã nhận job."""
    payload = {
        "status": "processing",
        "renderProvider": provider,
        "submittedAt": firestore.SERVER_TIMESTAMP,
        "updatedAt": firestore.SERVER_TIMESTAMP,
    }
    if provider == RENDER_PROVIDER_XIAOYANG:
        payload["xiaoyangTaskId"] = str(job_id)
        if xiaoyang_mode:
            payload["xiaoyangSubmitMode"] = xiaoyang_mode
        if xiaoyang_account:
            payload["xiaoyangAccount"] = str(xiaoyang_account)
        if xiaoyang_account_email:
            payload["xiaoyangAccountEmail"] = str(xiaoyang_account_email)
    elif provider == RENDER_PROVIDER_VIDEOAIEASY:
        payload["videoaieasyJobId"] = str(job_id)
        if videoaieasy_account:
            payload["videoaieasyAccount"] = str(videoaieasy_account)
        if videoaieasy_account_email:
            payload["videoaieasyAccountEmail"] = str(videoaieasy_account_email)
    else:
        payload["aidancingJobId"] = str(job_id)
    doc_ref.update(payload)


def _try_submit_xiaoyang(order_id) -> bool:
    account = _pick_xiaoyang_account()
    if not account:
        print(
            f"📊 XiaoYang đầy slot ({XIAOYANG_MAX_CONCURRENT_PER_ACCOUNT} đơn/nick) — {order_id}"
        )
        return False
    return submit_to_xiaoyang(order_id, account)


def _try_submit_videoaieasy(order_id) -> bool:
    if not _use_videoaieasy():
        return False
    account = _pick_videoaieasy_account()
    if not account:
        print(f"📊 Không có nick VideoAiEasy hoặc đầy slot — {order_id}")
        return False
    return submit_to_videoaieasy(order_id, account)


def submit_order(order_id):
    """Nạp đơn theo renderProvider trên đơn (nếu có) hoặc engine Admin."""
    doc_ref = db.collection("orders").document(order_id)
    doc = doc_ref.get()
    data = doc.to_dict() or {} if doc.exists else {}
    provider = (
        _order_render_provider(data)
        if data.get("renderProvider")
        else get_active_render_provider()
    )

    if provider == RENDER_PROVIDER_AIDANCING:
        submit_to_aidancing(order_id)
        return

    if provider == RENDER_PROVIDER_XIAOYANG:
        if _try_submit_xiaoyang(order_id):
            return
        print(f"⚠️ XiaoYang không nạp được {order_id} → thử VideoAiEasy")
        if _try_submit_videoaieasy(order_id):
            return
        print(f"⚠️ VideoAiEasy không nạp được {order_id} → chuyển Aidancing")
        submit_to_aidancing(order_id, fallback_reason="xiaoyang_fail")
        return

    if provider == RENDER_PROVIDER_VIDEOAIEASY:
        if _try_submit_videoaieasy(order_id):
            return
        print(f"⚠️ VideoAiEasy không nạp được {order_id} → thử XiaoYang")
        if _try_submit_xiaoyang(order_id):
            return
        print(f"⚠️ XiaoYang không nạp được {order_id} → chuyển Aidancing")
        submit_to_aidancing(order_id, fallback_reason="videoaieasy_fail")
        return

    submit_to_aidancing(order_id)


def submit_to_xiaoyang(order_id, account):
    if not is_bot_enabled():
        print(f"⏸️ [{BOT_NAME}] Bot TẮT — bỏ qua nạp đơn {order_id}")
        return False
    if _pending_submit_backoff_active(order_id):
        return False
    with _submitting_orders_lock:
        if order_id in _submitting_orders:
            print(f"⏭️ [{BOT_NAME}] Đơn {order_id} đang nạp — bỏ qua trùng lặp")
            return False
        _submitting_orders.add(order_id)

    account_id = account["id"]
    account_email = account.get("email", "")
    _xy_inflight_inc(account_id)
    success = False
    try:
        with _submit_engine_lock():
            doc_ref = db.collection("orders").document(order_id)
            doc = doc_ref.get()
            if not doc.exists:
                return False
            data = doc.to_dict() or {}
            if data.get("status") != "pending":
                return False

            xy_web = _use_xiaoyang_web_session()
            mode_label = "XiaoYang Web" if xy_web else "XiaoYang API"
            nick_label = account_email or account_id
            print(f"\n⚡ [NẠP ĐƠN / {mode_label}] {order_id} — nick {nick_label}...")
            img_url = (data.get("characterImageLink") or "").strip()
            vid_url = (data.get("referenceVideoLink") or "").strip()
            if not img_url or not vid_url:
                print(f"❌ Thiếu link ảnh/video cho đơn {order_id}")
                _fail_order_processing(
                    doc,
                    data,
                    "Thiếu characterImageLink hoặc referenceVideoLink",
                    "Thiếu ảnh hoặc video tham chiếu, hệ thống đã hoàn lại coin.",
                    "submit xiaoyang",
                )
                return False

            char_path = None
            vid_path = None
            try:
                modal, option = _xiaoyang_modal_for_order(data)
                prompt = (data.get("prompt") or get_env(
                    "XIAOYANG_PROMPT", "Follow the reference motion naturally"
                )).strip()
                tier = "Turbo/v3.0" if modal == XIAOYANG_MODAL_TURBO else "Thường/v2.6"

                if xy_web:
                    api = _get_xy_web_client(account_id)
                    _ensure_xy_web_session(api, account_email, account.get("password"))
                    enhance_4k = _xiaoyang_enhance_4k()
                    hd = " + HD 2K" if enhance_4k else ""
                    print(
                        f"🚀 [XiaoYang Web/{nick_label}] {tier}{hd} — "
                        f"modelId={data.get('modelId')} → {modal}/{option}..."
                    )
                    for attempt in range(1, 3):
                        if attempt > 1:
                            print(f"🔄 Thử tải file lần {attempt}...")
                        char_path = download_file(img_url, f"char_{order_id}.png")
                        vid_path = download_file(vid_url, f"vid_{order_id}.mp4")
                        if char_path and vid_path:
                            break
                        time.sleep(2)
                    if not char_path or not vid_path:
                        raise XiaoyangWebError("Không tải được ảnh/video từ link đơn hàng")
                    print("📤 Upload ảnh lên xiaoyang.online...")
                    image_token = api.upload_file(char_path)
                    print("📤 Upload video motion...")
                    video_token = api.upload_file(vid_path)
                    resp = api.create_motion_task(
                        image_token=image_token,
                        video_token=video_token,
                        prompt=prompt,
                        modal_key=modal,
                        option_key=option,
                        motion_orientation=get_env("XIAOYANG_MOTION_ORIENTATION", "video"),
                        enhance_4k=enhance_4k,
                    )
                    submit_mode = "web"
                else:
                    api = _get_xy_http_client()
                    from xiaoyang_direct import direct_worker_base

                    dw = direct_worker_base()
                    if dw:
                        print(f"📎 Direct worker: {dw}")
                    print(
                        f"🚀 [XiaoYang API] {tier} — modelId={data.get('modelId')} "
                        f"→ motion {modal}/{option}..."
                    )
                    resp = api.create_task(
                        modal,
                        option,
                        prompt,
                        image_url=img_url,
                        video_url=vid_url,
                        motion_orientation=get_env("XIAOYANG_MOTION_ORIENTATION", "video"),
                    )
                    submit_mode = "api"

                task_id = resp.get("task_id")
                if not task_id:
                    raise XiaoyangApiError(f"Không có task_id: {resp}")
                print(f"🆔 [XiaoYang/{nick_label}] task: {task_id} ({resp.get('status')})")
                _mark_order_processing(
                    doc_ref,
                    task_id,
                    provider=RENDER_PROVIDER_XIAOYANG,
                    xiaoyang_mode=submit_mode,
                    xiaoyang_account=account_id,
                    xiaoyang_account_email=account_email,
                )
                _session_error_backoff.pop(order_id, None)
                print(f"✅ Đơn {order_id} → processing ({mode_label}, {nick_label})")
                try:
                    short_id = order_id[-6:].upper()
                    send_telegram_message(
                        f"⚙️ <b>ĐƠN HÀNG ĐANG XỬ LÝ</b> (XiaoYang)\n\n"
                        f"🆔 Mã đơn: #{short_id}\n"
                        f"📧 Nick: {nick_label}\n"
                        f"🤖 Task: <code>{task_id}</code>\n"
                        f"⏳ Poll sau {MIN_RENDER_SEC // 60} phút..."
                    )
                except Exception:
                    pass
                success = True
            except (
                requests.RequestException,
                XiaoyangApiAuthError, XiaoyangApiError, XiaoyangWebAuthError, XiaoyangWebError,
                DirectMediaError, MediaValidationError, ValueError,
            ) as e:
                print(f"❌ Nạp XiaoYang thất bại {order_id} ({nick_label}): {e}")
                if isinstance(e, XiaoyangApiAuthError):
                    _reset_xy_http_client()
                if isinstance(e, XiaoyangWebAuthError):
                    _reset_xy_web_client(account_id)
                notify_internal_error_telegram(order_id, data, str(e), f"submit xiaoyang/{nick_label}")
                success = False
            finally:
                if char_path and os.path.exists(char_path):
                    os.remove(char_path)
                if vid_path and os.path.exists(vid_path):
                    os.remove(vid_path)
    finally:
        _xy_inflight_dec(account_id)
        with _submitting_orders_lock:
            _submitting_orders.discard(order_id)
    return success


def submit_to_videoaieasy(order_id, account):
    if not is_bot_enabled():
        print(f"⏸️ [{BOT_NAME}] Bot TẮT — bỏ qua nạp đơn {order_id}")
        return False
    if _pending_submit_backoff_active(order_id):
        return False
    with _submitting_orders_lock:
        if order_id in _submitting_orders:
            print(f"⏭️ [{BOT_NAME}] Đơn {order_id} đang nạp — bỏ qua trùng lặp")
            return False
        _submitting_orders.add(order_id)

    account_id = account["id"]
    account_email = account.get("email", "")
    _vae_inflight_inc(account_id)
    success = False
    try:
        with _submit_engine_lock():
            doc_ref = db.collection("orders").document(order_id)
            doc = doc_ref.get()
            if not doc.exists:
                return False
            data = doc.to_dict() or {}
            if data.get("status") != "pending":
                return False

            nick_label = account_email or account_id
            print(f"\n⚡ [NẠP ĐƠN / VideoAiEasy] {order_id} — nick {nick_label}...")
            img_url = (data.get("characterImageLink") or "").strip()
            vid_url = (data.get("referenceVideoLink") or "").strip()
            if not img_url or not vid_url:
                print(f"❌ Thiếu link ảnh/video cho đơn {order_id}")
                return False

            char_path = None
            vid_path = None
            vae_char_path = None
            vae_char_tmp = False
            try:
                model_id = _videoaieasy_model_for_order(data)
                resolution = resolution_for_order(data)
                duration_sec = duration_for_order(data)
                tier = "Kling 3.0" if model_id == MODEL_KLING_30 else "Kling 2.6"
                prompt = (data.get("prompt") or get_env(
                    "VIDEOAIEASY_PROMPT", "Follow the reference motion naturally"
                )).strip()
                api = _get_vae_web_client(account_id)
                _ensure_vae_web_session(api, account_email, account.get("password"))
                print(
                    f"🚀 [VideoAiEasy/{nick_label}] {tier} — "
                    f"modelId={data.get('modelId')} → {model_id} "
                    f"{duration_sec}s {resolution}..."
                )
                for attempt in range(1, 3):
                    if attempt > 1:
                        print(f"🔄 Thử tải file lần {attempt}...")
                    char_path = download_file(img_url, f"char_{order_id}.png")
                    vid_path = download_file(vid_url, f"vid_{order_id}.mp4")
                    if char_path and vid_path:
                        break
                    time.sleep(2)
                if not char_path or not vid_path:
                    raise VideoAiEasyError("Không tải được ảnh/video từ link đơn hàng")
                aspect = (data.get("aspectRatio") or "").strip() or "9:16"
                vae_char_path, vae_char_tmp = prepare_character_image_for_vae(
                    char_path, aspect_ratio=aspect
                )
                print("📤 Upload ảnh lên videoaieasy.hdgr.online...")
                image_url = api.upload_file(vae_char_path, kind="image")
                print("📤 Upload video motion...")
                video_url = api.upload_file(vid_path, kind="video")
                job_id = api.create_motion_job(
                    input_image_url=image_url,
                    driving_video_url=video_url,
                    prompt=prompt,
                    model_id=model_id,
                    resolution=resolution,
                    duration_sec=duration_sec,
                )
                print(f"🆔 [VideoAiEasy/{nick_label}] job: {job_id}")
                _mark_order_processing(
                    doc_ref,
                    job_id,
                    provider=RENDER_PROVIDER_VIDEOAIEASY,
                    videoaieasy_account=account_id,
                    videoaieasy_account_email=account_email,
                )
                _session_error_backoff.pop(order_id, None)
                print(f"✅ Đơn {order_id} → processing (VideoAiEasy, {nick_label})")
                try:
                    short_id = order_id[-6:].upper()
                    send_telegram_message(
                        f"⚙️ <b>ĐƠN HÀNG ĐANG XỬ LÝ</b> (VideoAiEasy)\n\n"
                        f"🆔 Mã đơn: #{short_id}\n"
                        f"📧 Nick: {nick_label}\n"
                        f"🤖 Job: <code>{job_id}</code>\n"
                        f"⏳ Poll sau {VIDEOAIEASY_MIN_RENDER_SEC // 60} phút, mỗi "
                        f"{VIDEOAIEASY_POLL_INTERVAL_SEC}s..."
                    )
                except Exception:
                    pass
                success = True
            except (requests.RequestException, VideoAiEasyAuthError, VideoAiEasyError) as e:
                print(f"❌ Nạp VideoAiEasy thất bại {order_id} ({nick_label}): {e}")
                if isinstance(e, VideoAiEasyAuthError):
                    _reset_vae_web_client(account_id)
                notify_internal_error_telegram(
                    order_id, data, str(e), f"submit videoaieasy/{nick_label}"
                )
                success = False
            finally:
                if vae_char_tmp and vae_char_path and os.path.exists(vae_char_path):
                    os.remove(vae_char_path)
                if char_path and os.path.exists(char_path):
                    os.remove(char_path)
                if vid_path and os.path.exists(vid_path):
                    os.remove(vid_path)
    finally:
        _vae_inflight_dec(account_id)
        with _submitting_orders_lock:
            _submitting_orders.discard(order_id)
    return success


def submit_to_aidancing(order_id, fallback_reason=None):
    if not is_bot_enabled():
        print(f"⏸️ [{BOT_NAME}] Bot TẮT — bỏ qua nạp đơn {order_id}")
        return
    if _pending_submit_backoff_active(order_id):
        return
    with _submitting_orders_lock:
        if order_id in _submitting_orders:
            print(f"⏭️ [{BOT_NAME}] Đơn {order_id} đang nạp — bỏ qua trùng lặp")
            return
        _submitting_orders.add(order_id)
    try:
        with _submit_engine_lock():
            doc_ref = db.collection('orders').document(order_id)
            doc = doc_ref.get()
            if not doc.exists:
                return
            data = doc.to_dict()
            if data.get('status') != 'pending':
                return

            fb = f" [fallback: {fallback_reason}]" if fallback_reason else ""
            print(f"\n⚡ [NẠP ĐƠN / Aidancing]{fb} {order_id}...")

            char_path = None
            vid_path = None

            # Thử tải tối đa 2 lần
            for attempt in range(1, 3):
                if attempt > 1: print(f"🔄 Thử lại lần {attempt}...")
                char_path = download_file(data.get('characterImageLink'), f"char_{order_id}.png")
                vid_path = download_file(data.get('referenceVideoLink'), f"vid_{order_id}.mp4")

                if char_path and vid_path:
                    break
                time.sleep(2)

            if not char_path or not vid_path:
                print(f"❌ Không thể tải file sau 2 lần thử cho đơn {order_id}")
                # Hoàn tiền cho khách
                cost_coins = data.get('costCoins', 0)
                user_id = data.get('userId')
                if cost_coins > 0 and user_id:
                    try:
                        db.collection('users').document(user_id).update({
                            'coins': firestore.Increment(cost_coins)
                        })
                        print(f"💰 Đã hoàn lại {cost_coins} coin cho user {user_id}")
                    except Exception as e:
                        print(f"⚠️ Lỗi khi hoàn tiền cho user {user_id}: {e}")

                doc_ref.update({
                    'status': 'failed',
                    'adminNote': firestore.DELETE_FIELD,
                    'systemNote': USER_NOTE_FILES_MISSING,
                    'updatedAt': firestore.SERVER_TIMESTAMP
                })

                # Gửi thông báo Telegram: Đơn hàng thất bại
                try:
                    short_id = order_id[-6:].upper()
                    user_name = data.get('userName', 'Khách hàng')
                    user_email = data.get('userEmail', 'N/A')
                    msg = (
                        f"❌ <b>ĐƠN HÀNG THẤT BẠI</b>\n\n"
                        f"🆔 Mã đơn: #{short_id}\n"
                        f"👤 Khách: {user_name}\n"
                        f"📧 Email: {user_email}\n"
                        f"📝 Lý do: Không thể tải ảnh/video nhân vật quý khách tải lên."
                    )
                    send_telegram_message(msg)
                except Exception as tele_err:
                    print(f"⚠️ Lỗi gửi thông báo Telegram thất bại: {tele_err}")
                if char_path and os.path.exists(char_path): os.remove(char_path)
                if vid_path and os.path.exists(vid_path): os.remove(vid_path)
                return

            if use_api_mode():
                try:
                    model_id = data.get('modelId', '124')
                    print(f"🚀 [HTTP] Nạp đơn model {model_id}...")
                    job_id = _http_create_job(model_id, char_path, vid_path)
                    print(f"🆔 [HTTP] Job mới: {job_id}")
                    _mark_order_processing(doc_ref, job_id)
                    _session_error_backoff.pop(order_id, None)
                    print(f"✅ Đơn {order_id} → processing (aidancing đã nhận job)")
                    try:
                        short_id = order_id[-6:].upper()
                        send_telegram_message(
                            f"⚙️ <b>ĐƠN HÀNG ĐANG XỬ LÝ</b>\n\n"
                            f"🆔 Mã đơn: #{short_id}\n"
                            f"🤖 Job ID aidancing: <code>{job_id}</code>\n"
                            f"⏳ Đang render (HTTP mode)..."
                        )
                    except Exception:
                        pass
                except SessionExpiredError as e:
                    print(f"❌ Session hết hạn: {e}")
                    _reset_http_client()
                    apply_bot_error_update(doc_ref, order_id, data, str(e), 'nạp HTTP')
                except Exception as e:
                    print(f"❌ Lỗi nạp HTTP: {e}")
                    err = str(e)
                    if any(x in err.lower() for x in ('401', '403', 'session expired', 'aidancing_cookie')):
                        _reset_http_client()
                    apply_bot_error_update(doc_ref, order_id, data, err, 'nạp HTTP')
                finally:
                    if char_path and os.path.exists(char_path):
                        os.remove(char_path)
                    if vid_path and os.path.exists(vid_path):
                        os.remove(vid_path)
                return

            def _pw_browser_submit():
                with sync_playwright() as p:
                    browser = launch_aidancing_browser(p)
                    page = browser.new_page()
                    try:
                        print("🌐 Đang kiểm tra danh sách Job cũ trên Dashboard...")
                        goto_aidancing_dashboard(page, browser)
                        balance = scrape_aidancing_balance(page)
                        if balance is not None:
                            print(f"💰 Aidancing balance: {balance} Coin")
                        if balance is not None and balance < AIDANCING_LOW_BALANCE_THRESHOLD:
                            short_id = order_id[-6:].upper()
                            user_name = data.get('userName', 'Khách hàng')
                            alert_low_aidancing_balance(
                                balance,
                                extra=f"\n📋 Bot đang nạp đơn: #{short_id}\n👤 Khách: {user_name}"
                            )
                        old_job_ids = set(re.findall(r'\b\d{6}\b', page.content()))
                        print(f"📦 Đã ghi nhận {len(old_job_ids)} Job ID cũ.")
                        model_id = data.get('modelId', '124')
                        create_url = f"{AIDANCING_ORIGIN}/create/general?id={model_id}"
                        print(f"🌐 Vào trang tạo: {create_url}")
                        page.goto(create_url, timeout=90000)
                        page.set_input_files('input[name="image"]', char_path)
                        page.set_input_files('input[name="video"]', vid_path)
                        page.locator('button.neon-ai-2').first.click()
                        print("⏳ Đợi chuyển về Dashboard và quét Job ID mới...")
                        page.wait_for_url("**/dashboard**", timeout=60000)
                        job_id = None
                        for _ in range(15):
                            page.wait_for_timeout(2000)
                            current_job_ids = set(re.findall(r'\b\d{6}\b', page.content()))
                            new_jobs = current_job_ids - old_job_ids
                            if new_jobs:
                                job_id = sorted(list(new_jobs))[-1]
                                break
                        if not job_id:
                            print("⚠️ Không tìm thấy Job ID mới sau 30s! Dùng cách lấy mặc định...")
                            job_ids = re.findall(r'\b\d{6}\b', page.content())
                            if job_ids:
                                job_id = job_ids[0]
                                print(f"🆔 LẤY ĐƯỢC JOB ID (Fallback): {job_id}")
                        return job_id
                    finally:
                        browser.close()

            try:
                job_id = run_playwright(_pw_browser_submit)
                if job_id:
                    print(f"🆔 LẤY ĐƯỢC JOB ID MỚI: {job_id}")
                    _mark_order_processing(doc_ref, job_id)
                    _session_error_backoff.pop(order_id, None)
                    print(f"✅ Đơn {order_id} → processing (aidancing đã nhận job)")
                    try:
                        short_id = order_id[-6:].upper()
                        user_name = data.get('userName', 'Khách hàng')
                        user_email = data.get('userEmail', 'N/A')
                        msg = (
                            f"⚙️ <b>ĐƠN HÀNG ĐANG XỬ LÝ</b>\n\n"
                            f"🆔 Mã đơn: #{short_id}\n"
                            f"👤 Khách: {user_name}\n"
                            f"📧 Email: {user_email}\n"
                            f"🤖 Job ID aidancing: <code>{job_id}</code>\n"
                            f"⏳ Đang render trên aidancing.net..."
                        )
                        send_telegram_message(msg)
                    except Exception as tele_err:
                        print(f"⚠️ Lỗi gửi thông báo Telegram xử lý: {tele_err}")
                else:
                    err = 'Bot nạp xong nhưng không lấy được Job ID aidancing — vẫn pending, thử lại sau.'
                    apply_bot_error_update(doc_ref, order_id, data, err, 'nạp browser')
            except Exception as e:
                print(f"❌ Lỗi nạp: {e}")
                apply_bot_error_update(doc_ref, order_id, data, str(e), 'nạp browser')
            finally:
                if os.path.exists(char_path):
                    os.remove(char_path)
                if os.path.exists(vid_path):
                    os.remove(vid_path)
    finally:
        with _submitting_orders_lock:
            _submitting_orders.discard(order_id)

# --- PHA 2: RÌNH KẾT QUẢ ---
def check_finished_orders():
    if use_api_mode():
        try:
            check_finished_orders_api()
        except Exception as e:
            print(f"❌ Lỗi monitor API: {e}")
        return
    if not is_bot_enabled():
        return
    try:
        # Nếu đang nạp đơn thì không check dashboard để tránh khóa profile
        if browser_lock.locked():
            return

        ad_orders, _, _, _, _ = _processing_monitor_state()
        if not ad_orders:
            return

        print(f"\n🔍 [MONITOR] Đang rình kết quả Aidancing cho {len(ad_orders)} đơn đủ {MIN_RENDER_SEC // 60}p...")
        orders_to_check = ad_orders
        with browser_lock:
            with sync_playwright() as p:
                browser = launch_aidancing_browser(p)
                page = browser.new_page()
                try:
                    goto_aidancing_dashboard(page, browser)
                except RuntimeError as e:
                    print(f"⚠️ {e}")
                    time.sleep(60)
                    browser.close()
                    return
                print(f"🌐 Đang ở: {page.url}")
                time.sleep(10)

                for doc in orders_to_check:
                    job_id = str(doc.to_dict().get('aidancingJobId'))
                    print(f"🧐 Đang tìm Job {job_id}...")

                    # Thử tìm text trong toàn bộ trang
                    if job_id not in page.content():
                        print(f"❌ Không thấy mã {job_id} trên trang này. Kiểm tra xem Job có ở trang 2 không?")
                        continue

                    # [FIX]: Tìm chính xác thẻ Card chứa đơn hàng này bằng cách mở rộng dần từ phần tử nhỏ nhất
                    # Đảm bảo không bao giờ bị dính vào thẻ List to đùng chứa nhiều đơn hàng (khiến cho bị nhận nhầm trạng thái của đơn khác)
                    containers = page.locator(f'div:has-text("{job_id}")')
                    count = containers.count()
                    card = None
                    
                    for i in range(count - 1, -1, -1):
                        container = containers.nth(i)
                        text = container.inner_text()
                        
                        # Đếm số lượng Job ID (6 số) trong thẻ này
                        ids_inside = set(re.findall(r'\b\d{6}\b', text))
                        if len(ids_inside) > 1:
                            # Nếu thẻ chứa nhiều hơn 1 đơn hàng -> Nó là thẻ List cha. Dừng lại, dùng thẻ con trước đó.
                            break
                        card = container

                    if card and card.is_visible():
                        text = card.inner_text()
                        # [FIX]: Bỏ "Tải Xuống" và "Download" khỏi điều kiện vì nút này luôn hiển thị trên UI kể cả khi đang xử lý
                        if any(x in text for x in ["Đã xong", "Success"]):
                            print(f"🎉 Job {job_id} HOÀN TẤT! Đang xử lý...")
                            # ... (giữ nguyên logic xử lý thành công)
                            try:
                                # Bước 1: Thử lấy link trực tiếp từ nút Tải TRONG CARD NÀY
                                ext_url = None
                                video_element = card.locator('video source, video[src]').first
                                if video_element.count() > 0 and video_element.is_visible():
                                    ext_url = video_element.get_attribute('src') or video_element.get_attribute('currentSrc')

                                download_link = card.locator(
                                    'a[href*="proxy/files"], a[href*="download"], a:has-text("Tải"), a:has-text("Download")'
                                ).first
                                if not ext_url and download_link.count() > 0 and download_link.is_visible():
                                    ext_url = download_link.get_attribute('href', timeout=3000)

                                # Bước 3 (Dự phòng): Click vào card để vào trang chi tiết lấy video
                                if not ext_url:
                                    try:
                                        print(f"🖱️ Click vào Job {job_id} để lấy link video...")
                                        card.click()
                                        page.wait_for_timeout(5000)
                                        # [FIX]: Kiểm tra xem trang CÓ THỰC SỰ CHUYỂN HAY KHÔNG
                                        if "dashboard" not in page.url:
                                            video_element = page.locator('video source, video[src]').first
                                            if video_element.count() > 0:
                                                ext_url = video_element.get_attribute('src')
                                            page.goto(DASHBOARD_URL) # Quay lại Dashboard
                                            time.sleep(3)
                                        else:
                                            print(f"❌ Nút click không chuyển trang. Bỏ qua để tránh lấy nhầm video ngoài Dashboard.")
                                    except Exception as e:
                                        print(f"❌ Lỗi khi vào trang chi tiết cho Job {job_id}: {e}")

                                # Bước 3: Tải file nếu đã có link (kèm cookies)
                                if ext_url:
                                    if not ext_url.startswith('http'):
                                        ext_url = AIDANCING_ORIGIN + ext_url

                                    dl_btn = download_link if (download_link.count() > 0 and ext_url) else None
                                    local_vid = download_aidancing_result(
                                        browser, page, ext_url, f"res_{doc.id}.mp4", download_locator=dl_btn
                                    )
                                    if local_vid:
                                        r2_url = upload_to_r2(local_vid)
                                        if r2_url:
                                            db.collection('orders').document(doc.id).update({
                                                'status': 'completed',
                                                'resultLink': r2_url,
                                                'updatedAt': firestore.SERVER_TIMESTAMP
                                            })
                                            print(f"✅ ĐÃ TRẢ HÀNG CHO ĐƠN {doc.id}")
                                            
                                            # Gửi thông báo Telegram: Đơn hàng hoàn thành
                                            try:
                                                order_data = doc.to_dict()
                                                short_id = doc.id[-6:].upper()
                                                user_name = order_data.get('userName', 'Khách hàng')
                                                user_email = order_data.get('userEmail', 'N/A')
                                                char_img = order_data.get('characterImageLink', '')
                                                msg = (
                                                    f"✅ <b>ĐƠN HÀNG HOÀN THÀNH</b>\n\n"
                                                    f"🆔 Mã đơn: #{short_id}\n"
                                                    f"👤 Khách: {user_name}\n"
                                                    f"📧 Email: {user_email}\n"
                                                )
                                                if char_img:
                                                    msg += f"📸 Ảnh đầu vào: <a href=\"{char_img}\">Xem ảnh gốc</a>\n"
                                                msg += f"📹 Kết quả: <a href=\"{r2_url}\">Xem video kết quả</a>"
                                                send_telegram_message(msg)
                                            except Exception as tele_err:
                                                print(f"⚠️ Lỗi gửi thông báo Telegram hoàn thành: {tele_err}")

                                            # Gửi mail thông báo tự động cho khách hàng
                                            try:
                                                order_data = doc.to_dict()
                                                send_completion_email(doc.id, order_data, r2_url)
                                            except Exception as mail_err:
                                                print(f"⚠️ Không gửi được email thông báo: {mail_err}")
                                                
                                            os.remove(local_vid)
                            except Exception as e:
                                print(f"⚠️ Lỗi xử lý Job {job_id}: {e}")
                            finally:
                                close_extra_aidancing_tabs(browser, page)
                                if page.url != DASHBOARD_URL:
                                    try:
                                        page.goto(DASHBOARD_URL, wait_until='domcontentloaded', timeout=60000)
                                        time.sleep(2)
                                    except Exception:
                                        pass
                        elif any(x in text for x in ["Chưa thành công", "Thất bại", "Failed", "Error"]):
                            print(f"❌ Job {job_id} THẤT BẠI TRÊN AIDANCING!")
                            order_data = doc.to_dict()
                            
                            # Hoàn tiền cho khách
                            cost_coins = order_data.get('costCoins', 0)
                            user_id = order_data.get('userId')
                            if cost_coins > 0 and user_id:
                                try:
                                    db.collection('users').document(user_id).update({
                                        'coins': firestore.Increment(cost_coins)
                                    })
                                    print(f"💰 Đã hoàn lại {cost_coins} coin cho user {user_id}")
                                except Exception as e:
                                    print(f"⚠️ Lỗi khi hoàn tiền cho user {user_id}: {e}")

                            db.collection('orders').document(doc.id).update({
                                'status': 'failed',
                                'adminNote': firestore.DELETE_FIELD,
                                'systemNote': USER_NOTE_FILES_INVALID,
                                'updatedAt': firestore.SERVER_TIMESTAMP
                            })

                            # Gửi thông báo Telegram: Đơn hàng thất bại
                            try:
                                order_data = doc.to_dict()
                                short_id = doc.id[-6:].upper()
                                user_name = order_data.get('userName', 'Khách hàng')
                                user_email = order_data.get('userEmail', 'N/A')
                                msg = (
                                    f"❌ <b>ĐƠN HÀNG THẤT BẠI</b>\n\n"
                                    f"🆔 Mã đơn: #{short_id}\n"
                                    f"👤 Khách: {user_name}\n"
                                    f"📧 Email: {user_email}\n"
                                    f"📝 Lý do: Ảnh/video tham chiếu không hợp lệ."
                                )
                                send_telegram_message(msg)
                            except Exception as tele_err:
                                print(f"⚠️ Lỗi gửi thông báo Telegram thất bại: {tele_err}")
                        else:
                            print(f"⏳ Job {job_id} vẫn đang render...")
                browser.close()
    except Exception as e:
        print(f"❌ Lỗi monitor: {e}")

def on_pending_orders_snapshot(keys, changes, read_time):
    if not is_bot_enabled():
        return
    _ensure_pending_worker()
    with _pending_queue_lock:
        for ch in changes:
            if ch.type.name != 'ADDED':
                continue
            oid = ch.document.id
            with _submitting_orders_lock:
                if oid in _submitting_orders:
                    continue
            if oid not in _pending_order_queue:
                _pending_order_queue.append(oid)
                print(f"📋 Xếp hàng nạp đơn: {oid} (còn {len(_pending_order_queue)} trong queue)")


def _enqueue_pending_rescan():
    """Đưa đơn pending vào queue (khởi động bot hoặc retry sau lỗi mạng)."""
    if not is_bot_enabled():
        return
    _ensure_pending_worker()
    try:
        docs = db.collection('orders').where(
            filter=FieldFilter("status", "==", "pending")
        ).limit(20).stream()
        with _pending_queue_lock:
            for doc in docs:
                oid = doc.id
                if _pending_submit_backoff_active(oid):
                    continue
                with _submitting_orders_lock:
                    if oid in _submitting_orders:
                        continue
                if oid not in _pending_order_queue:
                    _pending_order_queue.append(oid)
                    print(f"🔄 Hàng đợi thử lại đơn pending: {oid}")
    except Exception as e:
        print(f"⚠️ rescan pending: {e}")


def _rescan_pending_orders_loop():
    """Thử lại đơn pending sau khi session Aidancing được sửa (mỗi 5 phút)."""
    while True:
        time.sleep(SESSION_ERROR_BACKOFF_SEC)
        _enqueue_pending_rescan()


_batch_channel_trigger_lock = threading.Lock()
_batch_channel_trigger_running = False


def _firestore_ts_seconds(ts) -> float:
    if ts is None:
        return 0.0
    if hasattr(ts, "timestamp"):
        return float(ts.timestamp())
    try:
        return float(ts)
    except (TypeError, ValueError):
        return 0.0


def _run_batch_channel_trigger():
    global _batch_channel_trigger_running
    with _batch_channel_trigger_lock:
        if _batch_channel_trigger_running:
            return
        _batch_channel_trigger_running = True
    try:
        import subprocess

        root = os.path.dirname(os.path.abspath(__file__))
        proc = subprocess.run(
            [sys.executable, os.path.join(root, "batch_channel.py"), "--poll-trigger"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=7200,
        )
        if proc.stdout:
            print(proc.stdout.rstrip())
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            print(f"⚠️ batch channel「Chạy thử ngay」exit {proc.returncode}: {err[:500]}")
    except Exception as e:
        print(f"⚠️ batch channel「Chạy thử ngay」: {e}")
    finally:
        with _batch_channel_trigger_lock:
            _batch_channel_trigger_running = False


def start_batch_channel_listener():
    """Admin bấm「Chạy thử ngay」trên web → bot chạy batch ngay (không chờ 3h)."""
    doc_ref = db.collection("batchChannelConfig").document("default")

    def on_snapshot(keys, changes, read_time):
        if not changes:
            return
        data = {}
        for change in changes:
            doc = change.document
            if getattr(doc, "exists", False):
                data = doc.to_dict() or {}
            break
        requested = data.get("runNowRequestedAt")
        if not requested:
            return
        handled = data.get("runNowHandledAt")
        if _firestore_ts_seconds(handled) >= _firestore_ts_seconds(requested):
            return
        print("🚀 batch channel — nhận lệnh「Chạy thử ngay」từ web")
        threading.Thread(target=_run_batch_channel_trigger, daemon=True).start()

    doc_ref.on_snapshot(on_snapshot)
    print("👂 Lắng nghe batchChannelConfig — nút「Chạy thử ngay」trên web")


def start_bot():
    global BOT_NAME
    parser = argparse.ArgumentParser(description='Wallpaper/Nhay Cloud order bot — aidancing.net')
    parser.add_argument('--name', required=True, help='Tên bot duy nhất (vd: aidancing-vps1, bot-may-nha)')
    parser.add_argument('--mode', choices=['browser', 'api', 'http'], default=None,
                        help='browser=Playwright; api/http=Pure HTTP (AIDANCING_COOKIE, không Chrome)')
    args = parser.parse_args()
    if args.mode:
        os.environ['BOT_MODE'] = args.mode
    BOT_NAME = normalize_bot_name(args.name)
    if not BOT_NAME:
        print("❌ Tên bot không hợp lệ. Dùng: python bot.py --name aidancing-vps1")
        sys.exit(1)

    print(f"📡 Wallpaper BOT [{BOT_NAME}] (v4.0 xy+ad - mode={os.environ.get('BOT_MODE', 'browser')}) đang khởi động...")
    cdp_url = os.environ.get("BOT_CDP_URL", "").strip()
    if cdp_url:
        if ensure_cdp_available(cdp_url):
            print(f"✅ Chrome CDP sẵn sàng: {cdp_url}")
        else:
            print(f"⚠️  BOT_CDP_URL={cdp_url} nhưng Chrome chưa mở CDP!")
            print("    → Mở Chrome CDP ở terminal KHÁC trước, giữ chạy, rồi bot mới nối được.")
    start_bot_control_listener()
    start_render_provider_listener()
    start_processing_listener()
    start_batch_channel_listener()

    if use_api_mode():
        try:
            _get_http_client()
            print("✅ Pure HTTP — AIDANCING_COOKIE (không cần Chrome/CDP)")
        except ValueError as e:
            print(f"⚠️  Chưa cấu hình cookie: {e}")
        if _use_xiaoyang_web_session():
            accounts = _load_xiaoyang_accounts()
            hd = "bật" if _xiaoyang_enhance_4k() else "tắt"
            print(
                f"👥 XiaoYang accounts: {len(accounts)} nick | "
                f"max {XIAOYANG_MAX_CONCURRENT_PER_ACCOUNT} đơn/nick | HD 2K: {hd}"
            )
            for acc in accounts:
                try:
                    xy = _get_xy_web_client(acc["id"])
                    me = _ensure_xy_web_session(xy, acc["email"], acc["password"])
                    active = _xy_active_count(acc["id"])
                    print(
                        f"  ✅ {acc['email']} | credits: {me.get('credits', '?')} | "
                        f"đang chạy: {active}/{XIAOYANG_MAX_CONCURRENT_PER_ACCOUNT}"
                    )
                except Exception as e:
                    print(f"  ⚠️  {acc['email']}: {e}")
            if _use_videoaieasy():
                vae_accounts = _load_videoaieasy_accounts()
                print(
                    f"👥 VideoAiEasy accounts: {len(vae_accounts)} nick | "
                    f"max {VIDEOAIEASY_MAX_CONCURRENT_PER_ACCOUNT} đơn/nick"
                )
                for acc in vae_accounts:
                    try:
                        vae = _get_vae_web_client(acc["id"])
                        profile = _ensure_vae_web_session(vae, acc["email"], acc["password"])
                        coins = profile.get("coins", 0)
                        active = _vae_active_count(acc["id"])
                        print(
                            f"  ✅ {acc['email']} | coins: {coins} | "
                            f"đang chạy: {active}/{VIDEOAIEASY_MAX_CONCURRENT_PER_ACCOUNT}"
                        )
                    except Exception as e:
                        print(f"  ⚠️  {acc['email']}: {e}")
        else:
            try:
                me = _get_xy_http_client().me()
                print(f"✅ XiaoYang API [{BOT_NAME}] — {me.get('email', '?')} | credits: {me.get('credits', '?')}")
                from xiaoyang_direct import direct_worker_base

                dw = direct_worker_base()
                print(f"✅ XiaoYang direct worker: {dw or '(chưa cấu hình — Workers ?file= sẽ lỗi)'}")
            except Exception as e:
                print(f"⚠️  XiaoYang API: {e}")

    def monitor_loop():
        while True:
            ad_eligible, xy_eligible, vae_eligible, processing, vae_processing = _processing_monitor_state()
            if is_bot_enabled():
                check_finished_orders()
            if use_api_mode():
                sleep_sec = _monitor_sleep_seconds(
                    len(ad_eligible) + len(xy_eligible) + len(vae_eligible),
                    processing,
                    vae_processing_count=vae_processing,
                )
            else:
                sleep_sec = 60 if processing else int(os.environ.get("BOT_POLL_IDLE_SEC", "300"))
            time.sleep(sleep_sec)

    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=_rescan_pending_orders_loop, daemon=True).start()

    db.collection('orders').where(filter=FieldFilter("status", "==", "pending")).on_snapshot(on_pending_orders_snapshot)
    _enqueue_pending_rescan()

    print(f"🟢 [{BOT_NAME}] Đang trực — lắng nghe Firestore (bật/tắt từ Admin)...")
    while True:
        time.sleep(1)

if __name__ == "__main__":
    start_bot()
