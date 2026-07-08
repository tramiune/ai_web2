#!/usr/bin/env python3
import base64
import json
import re
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from project_env import load_project_env, get_env  # noqa: E402

load_project_env()
raw = get_env("AIDANCING_COOKIE") or ""
parts = dict(p.strip().split("=", 1) for p in raw.split(";") if "=" in p)
token = parts.get("ACCESS_TOKEN", "")
if token:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    print("JWT:", json.loads(base64.urlsafe_b64decode(payload)))

h = {"Cookie": raw, "User-Agent": "Mozilla/5.0", "Referer": "https://aidancing.net/en/dashboard"}
for path in ("/dashboard", "/en/dashboard"):
    r = requests.get(f"https://aidancing.net{path}", headers=h, timeout=20)
    html = r.text or ""
    print(f"\n{path} len={len(html)}")
    emails = re.findall(r"[\w.+-]+@[\w.-]+\.\w+", html)
    print("emails found:", sorted(set(emails))[:10])
    for kw in ("user-menu", "user-name", "user-email", "cp-balance"):
        i = html.find(kw)
        if i >= 0:
            print(kw, ":", html[i : i + 200].replace("\n", " "))
