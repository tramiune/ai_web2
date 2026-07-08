"""Client gọi RoboNeo relay trên Kaling bot (HTTP nội bộ VPS)."""

from __future__ import annotations

from typing import Any

import requests

from project_env import get_env, load_project_env

load_project_env()


def relay_configured() -> bool:
    return bool((get_env("KALING_ROBONEO_RELAY_URL") or "").strip()) and bool(
        (get_env("KALING_ROBONEO_RELAY_SECRET") or "").strip()
    )


def _base_url() -> str:
    return (get_env("KALING_ROBONEO_RELAY_URL") or "http://127.0.0.1:18765").rstrip("/")


def _secret() -> str:
    return (get_env("KALING_ROBONEO_RELAY_SECRET") or "").strip()


def _headers() -> dict[str, str]:
    return {"X-Relay-Secret": _secret(), "Content-Type": "application/json"}


def submit_order_via_relay(order_id: str, order_data: dict) -> dict[str, Any] | None:
    if not relay_configured():
        return None
    payload = {
        "secret": _secret(),
        "site": "nhay",
        "externalOrderId": order_id,
        "modelId": str(order_data.get("modelId") or ""),
        "characterImageLink": order_data.get("characterImageLink"),
        "referenceVideoLink": order_data.get("referenceVideoLink"),
        "prompt": order_data.get("prompt"),
        "maxVideoSec": order_data.get("maxVideoSec") or order_data.get("vaeDurationSec") or 10,
        "vaeDurationSec": order_data.get("vaeDurationSec") or 10,
        "vaeResolution": "720p",
    }
    r = requests.post(
        f"{_base_url()}/v1/roboneo/submit",
        json=payload,
        headers=_headers(),
        timeout=600,
    )
    r.raise_for_status()
    out = r.json()
    if not out.get("ok"):
        raise RuntimeError(out.get("error") or "relay submit failed")
    return out


def poll_relay(relay_id: str) -> dict[str, Any]:
    r = requests.get(
        f"{_base_url()}/v1/roboneo/jobs/{relay_id}",
        headers={"X-Relay-Secret": _secret()},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()


def download_relay_video(relay_id: str, dest_path: str) -> str:
    r = requests.get(
        f"{_base_url()}/v1/roboneo/jobs/{relay_id}/video",
        headers={"X-Relay-Secret": _secret()},
        timeout=600,
        stream=True,
    )
    r.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)
    return dest_path
