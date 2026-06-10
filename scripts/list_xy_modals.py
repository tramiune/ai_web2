#!/usr/bin/env python3
"""Liệt kê modal XiaoYang — tìm modal thay đồ."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from project_env import load_project_env

load_project_env()

from xiaoyang_api import XiaoyangApiClient


def main():
    api = XiaoyangApiClient()
    data = api.modals()
    raw = data.get("modals") or data.get("data") or data
    pairs: list[tuple[str, dict]] = []
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(v, dict):
                pairs.append((str(k), v))
    elif isinstance(raw, list):
        for m in raw:
            if isinstance(m, dict):
                pairs.append(
                    (
                        str(m.get("modal_key") or m.get("key") or m.get("id") or ""),
                        m,
                    )
                )
    print("TOTAL", len(pairs))
    keywords = ("cloth", "outfit", "wardrobe", "dress", "try", "wear", "fashion", "swap", "virtual", "thay")
    for key, m in pairs:
        if not isinstance(m, dict):
            print("SKIP", type(m), str(m)[:120])
            continue
        key = key or str(m.get("modal_key") or m.get("key") or m.get("id") or "")
        name = str(m.get("name") or m.get("title") or m.get("display_name") or m.get("label") or "")
        opts = m.get("options") or []
        if isinstance(opts, dict):
            opts = list(opts.values())
        opt_keys = [
            o.get("option_key") or o.get("key")
            for o in (opts[:8] if isinstance(opts, list) else [])
            if isinstance(o, dict)
        ]
        low = (key + " " + name).lower()
        tag = "WARDROBE?" if any(x in low for x in keywords) else "—"
        print(f"[{tag}] {key}\t{name}\topts={opt_keys}")
        if not key:
            print("  keys:", list(m.keys())[:20])
        if m.get("requires_clothes_image") or "wardrobe" in low:
            print("  FULL:", json.dumps(m, ensure_ascii=False)[:2000])


if __name__ == "__main__":
    main()
