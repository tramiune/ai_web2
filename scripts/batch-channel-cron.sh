#!/usr/bin/env bash
# Cron 3:00 sáng VN — batch kênh TikTok (Nhay Cloud)
# Crontab VPS (UTC+7): 0 3 * * * /home/hoang1432001/ai_web2/scripts/batch-channel-cron.sh >> /home/hoang1432001/ai_web2/logs/batch-channel.log 2>&1

set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
export PYTHONUNBUFFERED=1
python3 batch_channel.py
