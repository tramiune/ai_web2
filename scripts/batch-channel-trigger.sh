#!/usr/bin/env bash
# Poll Firestore runNowRequestedAt — admin bấm「Chạy thử ngay」trên web.
# Crontab: * * * * * /home/hoang1432001/ai_web2/scripts/batch-channel-trigger.sh >> /home/hoang1432001/ai_web2/logs/batch-channel-trigger.log 2>&1

set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
export PYTHONUNBUFFERED=1
python3 batch_channel.py --poll-trigger
