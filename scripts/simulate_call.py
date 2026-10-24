#!/usr/bin/env python3
"""Drive a fake Vapi call through the local webhook and /debug/control.

Start the app first:
  uvicorn app.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

BASE = os.getenv("FREQ_BASE", "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.getenv("WEBHOOK_TOKEN", "dev-webhook-token")
PHONE = os.getenv("FREQ_SIM_PHONE", "+18585550199")
# UCSD / Geisel, inside the demo box.
LAT, LON = 32.8801, -117.2340


def post(client: httpx.Client, path: str, payload: dict) -> httpx.Response:
    response = client.post(path, json=payload)
    print(f"POST {path} -> {response.status_code}")
    text = response.text
    print(text[:500])
    if response.status_code >= 400:
        sys.exit(1)
    return response


def main() -> int:
    webhook = f"/vapi/webhook/{TOKEN}"
    control = f"{BASE}/debug/control"
    call_id = "sim-call-freq"
    with httpx.Client(base_url=BASE, timeout=10.0) as client:
        try:
            health = client.get("/health")
        except httpx.HTTPError as exc:
            print(f"Freq is not reachable at {BASE} ({exc.__class__.__name__}). Start uvicorn first.", file=sys.stderr)
            return 1
        if health.status_code >= 400:
            print(health.text, file=sys.stderr)
            return 1
        post(client, "/api/loc", {"phone": PHONE, "lat": LAT, "lon": LON, "accuracy": 12})
        post(client, "/api/replay/start", {"phone": PHONE})
        post(
            client,
            webhook,
            {
                "message": {
                    "type": "status-update",
                    "status": "in-progress",
                    "call": {
                        "id": call_id,
                        "customer": {"number": PHONE},
                        "monitor": {"controlUrl": control},
                    },
                }
            },
        )
        print("waiting for the monitor to inject…")
        injections = []
        deadline = time.time() + 12
        while time.time() < deadline:
            time.sleep(1.5)
            listing = client.get("/debug/control")
            injections = (listing.json() or {}).get("injections") or []
            if injections:
                break
        print(f"debug injections: {len(injections)}")
        for item in injections:
            print(item.get("body"))
        if not injections:
            print("No injection reached /debug/control.", file=sys.stderr)
            return 1
        tools = post(
            client,
            webhook,
            {
                "message": {
                    "type": "tool-calls",
                    "call": {"id": call_id, "customer": {"number": PHONE}},
                    "toolCallList": [
                        {"id": "call-situation", "function": {"name": "get_situation", "arguments": {}}},
                        {"id": "call-ceiling", "function": {"name": "get_ceiling", "arguments": {}}},
                        {
                            "id": "call-traffic",
                            "function": {"name": "get_nearby_traffic", "arguments": {"radius_nm": 10}},
                        },
                        {"id": "call-weather", "function": {"name": "get_weather", "arguments": {}}},
                        {
                            "id": "call-rule",
                            "function": {"name": "set_rule", "arguments": {"radius_nm": 2, "quiet": False}},
                        },
                    ],
                }
            },
        )
        body = tools.json()
        results = body.get("results") or []
        if len(results) < 5:
            print("Tool webhook did not return one result per call.", file=sys.stderr)
            return 1
        blob = " ".join(str(item.get("result")) for item in results)
        if "N123HX" in blob:
            print("Tool text included a tail number.", file=sys.stderr)
            return 1
        post(
            client,
            webhook,
            {
                "message": {
                    "type": "end-of-call-report",
                    "call": {"id": call_id, "customer": {"number": PHONE}},
                }
            },
        )
    print("Simulated call completed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
