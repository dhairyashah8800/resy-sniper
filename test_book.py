#!/usr/bin/env python3
"""One-shot test: find first slot on a single date and book it, printing raw /3/book response."""

import json
import logging
import os
import sys
from datetime import datetime

import requests


def _require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        print(f"FATAL: environment variable {name} is not set.", file=sys.stderr)
        sys.exit(1)
    return val


API_KEY = _require_env("RESY_API_KEY")
AUTH_TOKEN = _require_env("RESY_AUTH_TOKEN")
PAYMENT_METHOD_ID = int(_require_env("RESY_PAYMENT_METHOD_ID"))

# ── Test config ───────────────────────────────────────────────────────────────
VENUE_ID = 75203
DAY = "2026-04-27"
PARTY_SIZE = 4
PREFERRED_START_HOUR = 17

BASE_URL = "https://api.resy.com"

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

BASE_HEADERS = {
    "Authorization": f'ResyAPI api_key="{API_KEY}"',
    "X-Resy-Auth-Token": AUTH_TOKEN,
    "X-Resy-Universal-Slot": "1",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://resy.com",
    "Referer": "https://resy.com/",
}
FORM_HEADERS = {**BASE_HEADERS, "Content-Type": "application/x-www-form-urlencoded"}


def main():
    # ── Step 1: find slots ────────────────────────────────────────────────────
    log.info(f"GET /4/find  venue={VENUE_ID}  day={DAY}  party={PARTY_SIZE}")
    r = requests.get(
        f"{BASE_URL}/4/find",
        params={"lat": "0", "long": "0", "day": DAY, "party_size": PARTY_SIZE, "venue_id": VENUE_ID},
        headers=BASE_HEADERS,
        timeout=15,
    )
    log.info(f"  status: {r.status_code}")
    r.raise_for_status()

    data = r.json()
    venues = data.get("results", {}).get("venues", [])
    if not venues:
        log.error("No venues returned — venue ID may be wrong or no availability at all")
        print("\nRaw /4/find response:")
        print(json.dumps(data, indent=2))
        return

    all_slots = venues[0].get("slots", [])
    log.info(f"  total slots returned: {len(all_slots)}")

    # ── Step 2: filter 17:00+ ─────────────────────────────────────────────────
    qualifying = []
    for slot in all_slots:
        start_str = slot.get("date", {}).get("start", "")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(start_str, fmt)
                break
            except ValueError:
                pass
        else:
            try:
                dt = datetime.fromisoformat(start_str)
            except ValueError:
                continue
        if dt.hour >= PREFERRED_START_HOUR:
            qualifying.append((dt, slot))

    qualifying.sort(key=lambda x: x[0])
    log.info(f"  slots at 17:00+: {len(qualifying)}")

    if not qualifying:
        log.error(f"No slots at {PREFERRED_START_HOUR}:00 or later on {DAY}")
        print("\nAll available slot times:")
        for s in all_slots:
            print(" ", s.get("date", {}).get("start"))
        return

    dt, slot = qualifying[0]
    slot_time = slot.get("date", {}).get("start")
    log.info(f"  Picking earliest qualifying slot: {slot_time}")

    # ── Step 3: get book_token ────────────────────────────────────────────────
    config = slot.get("config", {})
    config_id = config.get("token") or config.get("id")
    log.info(f"POST /3/details  config_id={config_id}")

    r2 = requests.post(
        f"{BASE_URL}/3/details",
        json={"config_id": config_id, "day": DAY, "party_size": PARTY_SIZE},
        headers=BASE_HEADERS,
        timeout=15,
    )
    log.info(f"  status: {r2.status_code}")
    if not r2.ok:
        log.error(f"  /3/details failed: {r2.text}")
        return

    details_data = r2.json()
    print("\nRaw /3/details response:")
    print(json.dumps(details_data, indent=2))

    raw_token = details_data.get("book_token", {})
    book_token = raw_token.get("value") if isinstance(raw_token, dict) else raw_token
    log.info(f"  book_token (full): {repr(book_token)}")

    if not book_token:
        log.error("No book_token in /3/details response")
        return

    # ── Step 4: book ─────────────────────────────────────────────────────────
    import json as _json
    struct_pm = _json.dumps({"id": PAYMENT_METHOD_ID, "object": "payment_method"})
    log.info(f"POST /3/book  struct_payment_method={struct_pm}")
    r3 = requests.post(
        f"{BASE_URL}/3/book",
        data={
            "book_token": book_token,
            "struct_payment_method": struct_pm,
            "source_id": "resy.com-venue-details",
        },
        headers=FORM_HEADERS,
        timeout=15,
    )
    log.info(f"  status: {r3.status_code}")

    print()
    print("=" * 60)
    print("  RAW /3/book RESPONSE")
    print("=" * 60)
    try:
        print(json.dumps(r3.json(), indent=2))
    except Exception:
        print(r3.text)
    print("=" * 60)

    if r3.ok:
        result = r3.json()
        conf = result.get("resy_token") or result.get("reservation_id") or result.get("confirmation") or "?"
        print()
        print("=" * 60)
        print("  BOOKED [OK]")
        print("=" * 60)
        print(f"  Date:         {DAY}")
        print(f"  Time:         {slot_time}")
        print(f"  Party size:   {PARTY_SIZE}")
        print(f"  Confirmation: {conf}")
        print("=" * 60)
    else:
        print(f"\nBooking did not succeed (HTTP {r3.status_code})")


if __name__ == "__main__":
    main()
