#!/usr/bin/env python3
"""
Demo bot Video AI Easy — luồng giống XiaoYang (login → upload ảnh/video → tạo job → poll → tải).

Không gắn Firestore / bot VPS. Chạy local để test.

  python demo/videoaieasy/demo_bot.py check
  python demo/videoaieasy/demo_bot.py motion --image char.png --video ref.mp4
  python demo/videoaieasy/demo_bot.py jobs
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Cho phép import videoaieasy_client khi chạy từ repo root
sys.path.insert(0, str(Path(__file__).resolve().parent))

from videoaieasy_client import (  # noqa: E402
    MODEL_KLING_26,
    MODEL_KLING_30,
    VideoAiEasyClient,
    VideoAiEasyError,
)

DEFAULT_EMAIL = os.environ.get("VIDEOAIEASY_EMAIL", "hoang@gmail.com")
DEFAULT_PASSWORD = os.environ.get("VIDEOAIEASY_PASSWORD", "123456")


def account_id_from_email(email: str) -> str:
    return (email.split("@")[0] or "default").lower()


def cmd_check(client: VideoAiEasyClient, email: str, password: str) -> int:
    print("🔐 Đăng nhập Video AI Easy...")
    profile = client.ensure_session(email, password)
    coins = profile.get("coins", 0)
    xu = client.coins_to_xu(coins)
    print(f"✅ {profile.get('email', email)} | coins={coins} ({xu} xu) | tier={profile.get('tier', '?')}")
    jobs = client.list_jobs()
    print(f"📋 Jobs hiện có: {len(jobs)}")
    for j in jobs[:5]:
        print(
            f"  - {j.get('id', '?')[:8]}… | {j.get('status')} | "
            f"{j.get('mode')} | {j.get('params', {}).get('modelId', '')}"
        )
    if coins <= 0:
        print("⚠️  Tài khoản 0 xu — upload OK nhưng tạo video sẽ bị chặn cho đến khi nạp.")
    return 0


def cmd_jobs(client: VideoAiEasyClient, email: str, password: str) -> int:
    client.ensure_session(email, password)
    jobs = client.list_jobs()
    if not jobs:
        print("(không có job)")
        return 0
    for j in jobs:
        print(
            f"{j.get('id')} | {j.get('status')} | {j.get('mode')} | "
            f"out={bool(j.get('output_video_url'))}"
        )
    return 0


def cmd_motion(args, client: VideoAiEasyClient, email: str, password: str) -> int:
    client.ensure_session(email, password)
    profile = client.get_profile()
    if (profile.get("coins") or 0) <= 0:
        print("❌ Không đủ xu để tạo video. Chạy `check` hoặc nạp trên web trước.")
        return 1

    model = MODEL_KLING_30 if args.turbo else MODEL_KLING_26
    tier = "Kling 3.0" if args.turbo else "Kling 2.6"
    print(f"📤 Upload ảnh: {args.image}")
    image_url = client.upload_file(args.image, kind="image")
    print(f"   → {image_url}")
    print(f"📤 Upload video motion: {args.video}")
    video_url = client.upload_file(args.video, kind="video")
    print(f"   → {video_url}")

    print(f"🚀 Tạo job motion-control ({tier})...")
    job_id = client.create_motion_job(
        input_image_url=image_url,
        driving_video_url=video_url,
        prompt=args.prompt,
        model_id=model,
    )
    print(f"🆔 jobId: {job_id}")

    def on_status(job):
        print(f"   status={job.get('status')} | charged={job.get('coins_charged')}")

    job = client.poll_job(job_id, on_status=on_status)
    out_url = job.get("output_video_url")
    print(f"🎉 Hoàn tất — {out_url}")

    if args.out:
        path = client.download_job(job_id, args.out)
        print(f"💾 Đã tải: {path}")
    return 0


def cmd_poll(args, client: VideoAiEasyClient, email: str, password: str) -> int:
    client.ensure_session(email, password)
    job = client.poll_job(args.job_id)
    print(job)
    if args.out and job.get("status") == "done":
        path = client.download_job(args.job_id, args.out)
        print(f"💾 Đã tải: {path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Demo bot Video AI Easy (giống XiaoYang)")
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="Login + xem số dư xu và jobs")

    p_jobs = sub.add_parser("jobs", help="Liệt kê jobs")

    p_motion = sub.add_parser("motion", help="Ảnh + video motion → tạo video (giống XiaoYang)")
    p_motion.add_argument("--image", required=True, help="Ảnh nhân vật (local path)")
    p_motion.add_argument("--video", required=True, help="Video tham chiếu chuyển động (local path)")
    p_motion.add_argument("--prompt", default="Follow the reference motion naturally")
    p_motion.add_argument("--turbo", action="store_true", help="Dùng Kling 3.0 thay vì 2.6")
    p_motion.add_argument("--out", default="", help="Lưu file MP4 (optional)")

    p_poll = sub.add_parser("poll", help="Poll job có sẵn")
    p_poll.add_argument("job_id")
    p_poll.add_argument("--out", default="")

    args = parser.parse_args()
    client = VideoAiEasyClient(account_id=account_id_from_email(args.email))

    try:
        if args.cmd == "check":
            return cmd_check(client, args.email, args.password)
        if args.cmd == "jobs":
            return cmd_jobs(client, args.email, args.password)
        if args.cmd == "motion":
            return cmd_motion(args, client, args.email, args.password)
        if args.cmd == "poll":
            return cmd_poll(args, client, args.email, args.password)
    except VideoAiEasyError as e:
        print(f"❌ {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
