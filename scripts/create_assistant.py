#!/usr/bin/env python3
"""Create or update the Freq Vapi assistant and attach it to the inbound number.

Safe to run with no key: prints a clear error and exits non-zero.
silenceTimeoutSeconds is sent once and dropped if Vapi returns 400.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

USER_AGENT = "Freq/0.1 (hackathon)"
API = "https://api.vapi.ai"
NAME = "Freq"

SYSTEM = """You are Freq, a calm aviation radio for a hobby drone pilot in San Diego.
Speak in at most two short sentences. Use feet and statute miles. Round altitudes to the nearest hundred feet.

Rules you do not break:
- You are advisory only. You are not air traffic control. You do not replace a visual observer.
- You only know aircraft that broadcast ADS-B. Say so if asked what you can see. Never invent traffic.
- Never read a tail number, registration, or hex code. Say "a helicopter" or "a light aircraft".
- If you do not have a position, tell the pilot to open the Freq link and tap Start flight. If their caller ID did not match, ask "What is your Freq code?" and call link_session with the 4 digits.
- Outside San Diego, say exactly: Freq demo covers San Diego only.
- When they are inside a LAANC grid or surface Class B, C, D, or surface Class E, never say they can fly up to a number. Say they need LAANC approval, and quote the facility-map ceiling. A ceiling of 0 means "Don't fly here."
- Class E shelves and a Class B floor overhead do not change the surface ceiling.
- Call get_situation as soon as the call connects, then give ceiling, traffic, and wind.
- Call set_rule when they change distance, altitude, aircraft type, or say to be quiet unless it is urgent.
- Glider and land-manager notes are advisories, not airspace.
"""


def tool(name: str, description: str, properties: dict | None = None, required: list | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": required or [],
            },
        },
    }


def payload(base_url: str, token: str, *, silence: bool) -> dict:
    body = {
        "name": NAME,
        "firstMessage": (
            "Freq on. This is advisory only, not air traffic control, and it does not replace a visual observer. "
            "I only know aircraft that broadcast ADS-B. Getting your position."
        ),
        "model": {
            "provider": "openai",
            "model": "gpt-4.1-mini",
            "messages": [{"role": "system", "content": SYSTEM}],
            "tools": [
                tool("get_situation", "Ceiling, the nearest aircraft, and wind from the cached session. Call this first."),
                tool("get_ceiling", "Allowed ceiling, airspace class, and whether LAANC is required."),
                tool(
                    "get_nearby_traffic",
                    "Aircraft Freq can see near the pilot. Omits tail numbers.",
                    {"radius_nm": {"type": "number", "description": "Optional radius in nautical miles."}},
                ),
                tool("get_weather", "Cached wind in knots. Gusts may be unavailable."),
                tool(
                    "set_rule",
                    "Change what the pilot wants to hear. radius_nm is nautical miles. "
                    "include_types and exclude_types take category codes like A7 or words like helicopter or airliner.",
                    {
                        "radius_nm": {"type": "number"},
                        "max_alt_ft": {"type": "number"},
                        "include_types": {"type": "string"},
                        "exclude_types": {"type": "string"},
                        "quiet": {"type": "boolean", "description": "True means alerts only."},
                    },
                ),
                tool(
                    "link_session",
                    "Attach this call to the 4-digit Freq code on the start page when caller ID did not match.",
                    {"code": {"type": "string", "description": "Four digits."}},
                    ["code"],
                ),
            ],
        },
        "transcriber": {
            "provider": "deepgram",
            "model": "nova-2",
            "keywords": ["LAANC:2", "ceiling:2", "helicopter:2", "descend:2"],
        },
        "maxDurationSeconds": 3600,
        "backgroundDenoisingEnabled": True,
        "monitorPlan": {"listenEnabled": False, "controlEnabled": True},
        "server": {"url": f"{base_url}/vapi/webhook/{token}"},
        "serverMessages": ["tool-calls", "status-update", "end-of-call-report"],
    }
    if silence:
        body["silenceTimeoutSeconds"] = 3600
    return body


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(1)


def main() -> int:
    key = os.getenv("VAPI_API_KEY", "").strip()
    if not key:
        fail("VAPI_API_KEY is not set. Add it to .env (do not commit that file) and run this script again.")
    phone_id = os.getenv("VAPI_PHONE_NUMBER_ID", "").strip()
    base = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    token = os.getenv("WEBHOOK_TOKEN", "").strip()
    if not base:
        fail("PUBLIC_BASE_URL is not set. Use your ngrok static domain, for example https://name.ngrok-free.app.")
    if not token:
        fail("WEBHOOK_TOKEN is not set. Put a long random string in .env. It is the webhook path, not a Vapi secret.")
    if token == "dev-webhook-token":
        print("Warning: WEBHOOK_TOKEN is still the local default. Set a random token before a public demo.", file=sys.stderr)
    if not base.startswith("https://"):
        print(f"Warning: PUBLIC_BASE_URL is {base}. Vapi needs a public https URL.", file=sys.stderr)

    headers = {"Authorization": f"Bearer {key}", "User-Agent": USER_AGENT}
    with httpx.Client(headers=headers, timeout=30.0) as client:
        existing = client.get(f"{API}/assistant")
        if existing.status_code >= 400:
            fail(f"Could not list assistants ({existing.status_code}): {existing.text[:400]}")
        assistants = existing.json()
        if isinstance(assistants, dict):
            assistants = assistants.get("results") or assistants.get("assistants") or []
        match = next((item for item in assistants if item.get("name") == NAME), None)
        body = payload(base, token, silence=True)
        if match:
            response = client.patch(f"{API}/assistant/{match['id']}", json=body)
            action = "updated"
        else:
            response = client.post(f"{API}/assistant", json=body)
            action = "created"
        if response.status_code == 400 and "silenceTimeoutSeconds" in body:
            print("Vapi rejected silenceTimeoutSeconds. Retrying without it.", file=sys.stderr)
            body = payload(base, token, silence=False)
            if match:
                response = client.patch(f"{API}/assistant/{match['id']}", json=body)
            else:
                response = client.post(f"{API}/assistant", json=body)
        if response.status_code >= 400:
            fail(f"Assistant {action} failed ({response.status_code}): {response.text[:800]}")
        assistant = response.json()
        assistant_id = assistant.get("id") or (match or {}).get("id")
        print(f"Freq assistant {action}: {assistant_id}")
        if not phone_id:
            fail("Assistant is saved, but VAPI_PHONE_NUMBER_ID is not set, so the inbound number was not updated.")
        patched = client.patch(f"{API}/phone-number/{phone_id}", json={"assistantId": assistant_id})
        if patched.status_code >= 400:
            fail(f"Phone number update failed ({patched.status_code}): {patched.text[:800]}")
        print(f"Attached assistant {assistant_id} to phone number {phone_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
