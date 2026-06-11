#!/usr/bin/env bash
# Cron 3:00 sáng VN — batch kênh TikTok (Nhay Cloud)
# Crontab VPS (UTC): dùng CRON_TZ=Asia/Ho_Chi_Minh + 0 3 * * *

set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
export PYTHONUNBUFFERED=1
python3 batch_channel.py
