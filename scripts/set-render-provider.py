#!/usr/bin/env python3
"""Đặt activeRenderProvider trên bots/{bot_id} (service account)."""
import argparse
import os
import sys

import firebase_admin
from firebase_admin import credentials, firestore

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--provider",
        choices=["aidancing", "xiaoyang", "videoaieasy"],
        default="xiaoyang",
    )
    parser.add_argument("--bot-id", default="nhaycloud_vps_bot")
    args = parser.parse_args()

    key_path = os.path.join(ROOT, "serviceAccountKey.json")
    if not os.path.isfile(key_path):
        print(f"Missing {key_path}", file=sys.stderr)
        sys.exit(1)

    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(key_path))
    db = firestore.client()
    ref = db.collection("bots").document(args.bot_id)
    ref.set(
        {
            "activeRenderProvider": args.provider,
            "updatedBy": "set-render-provider.py",
        },
        merge=True,
    )
    print(f"OK bots/{args.bot_id} activeRenderProvider={args.provider}")


if __name__ == "__main__":
    main()
