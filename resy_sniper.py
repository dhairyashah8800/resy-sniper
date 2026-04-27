#!/usr/bin/env python3
"""Resy reservation sniper — polls for slots and books immediately when found."""

import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from typing import Optional

import requests

# ── Hardcoded target config ───────────────────────────────────────────────────
API_KEY = "REDACTED_API_KEY"
VENUE_ID = 94741
PARTY_SIZE = 4
PREFERRED_START_HOUR = 17  # 5:00 PM

START_DATE = date(2026, 5, 14)
END_DATE = date(2026, 5, 24)
POLL_INTERVAL = 60  # seconds between full date-range cycles

BASE_URL = "https://api.resy.com"
TELEGRAM_API = "https://api.telegram.org"

# ── ENV vars (required) ───────────────────────────────────────────────────────
def _require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        print(f"FATAL: environment variable {name} is not set.", file=sys.stderr)
        sys.exit(1)
    return val

AUTH_TOKEN = _require_env("RESY_AUTH_TOKEN")
PAYMENT_METHOD_ID = int(_require_env("RESY_PAYMENT_METHOD_ID"))
TELEGRAM_BOT_TOKEN = _require_env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _require_env("TELEGRAM_CHAT_ID")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ── Headers (built after ENV vars load) ──────────────────────────────────────
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


# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(msg: str) -> None:
    try:
        requests.post(
            f"{TELEGRAM_API}/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as exc:
        log.warning(f"Telegram send failed: {exc}")


# ── Date helpers ──────────────────────────────────────────────────────────────
def all_dates() -> list[str]:
    result = []
    d = START_DATE
    while d <= END_DATE:
        result.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return result


def in_target_range(day_str: str) -> bool:
    """Return True only if day_str falls within [START_DATE, END_DATE]."""
    try:
        d = datetime.strptime(day_str[:10], "%Y-%m-%d").date()
        return START_DATE <= d <= END_DATE
    except ValueError:
        return False


# ── API calls ─────────────────────────────────────────────────────────────────
def find_slots(day: str) -> Optional[list]:
    """Returns slot list, None on rate-limit, [] when nothing available."""
    resp = requests.get(
        f"{BASE_URL}/4/find",
        params={
            "lat": "0",
            "long": "0",
            "day": day,
            "party_size": PARTY_SIZE,
            "venue_id": VENUE_ID,
        },
        headers=BASE_HEADERS,
        timeout=15,
    )
    if resp.status_code == 429:
        return None
    resp.raise_for_status()
    data = resp.json()
    venues = data.get("results", {}).get("venues", [])
    if not venues:
        return []
    return venues[0].get("slots", [])


def get_book_token(slot: dict, day: str) -> Optional[str]:
    """Returns book_token string, or None on failure/rate-limit."""
    config = slot.get("config", {})
    config_id = config.get("token") or config.get("id")
    resp = requests.post(
        f"{BASE_URL}/3/details",
        json={"config_id": config_id, "day": day, "party_size": PARTY_SIZE},
        headers=BASE_HEADERS,
        timeout=15,
    )
    if resp.status_code == 429:
        return None
    if not resp.ok:
        log.warning(f"  /3/details {resp.status_code}: {resp.text[:300]}")
        return None
    data = resp.json()
    raw = data.get("book_token", {})
    return raw.get("value") if isinstance(raw, dict) else raw


def book_slot(book_token: str) -> Optional[dict]:
    """Returns response dict, {"race": True} on 409/412, None on other failure."""
    struct_pm = json.dumps({"id": PAYMENT_METHOD_ID, "object": "payment_method"})
    resp = requests.post(
        f"{BASE_URL}/3/book",
        data={
            "book_token": book_token,
            "struct_payment_method": struct_pm,
            "source_id": "resy.com-venue-details",
        },
        headers=FORM_HEADERS,
        timeout=15,
    )
    if resp.status_code == 429:
        return None
    if resp.status_code in (409, 412):
        return {"race": True}
    if not resp.ok:
        log.warning(f"  /3/book {resp.status_code}: {resp.text[:300]}")
        return None
    return resp.json()


# ── Slot filtering ────────────────────────────────────────────────────────────
def qualifying_slots(slots: list) -> list:
    """Return slots at or after PREFERRED_START_HOUR, sorted earliest first."""
    out = []
    for slot in slots:
        start_str = slot.get("date", {}).get("start", "")
        if not start_str:
            continue
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
                log.debug(f"  Could not parse date: {start_str!r}")
                continue
        if dt.hour >= PREFERRED_START_HOUR:
            out.append((dt, slot))
    out.sort(key=lambda x: x[0])
    return [s for _, s in out]


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    dates = all_dates()
    log.info(
        f"Resy sniper started — venue {VENUE_ID}, party of {PARTY_SIZE}, "
        f"{dates[0]} to {dates[-1]}, slots from {PREFERRED_START_HOUR}:00 onward"
    )
    log.info(f"Cycling through {len(dates)} dates every {POLL_INTERVAL}s")

    backoff = 0

    while True:
        for day in dates:
            if backoff:
                log.warning(f"Rate-limited — sleeping {backoff}s")
                time.sleep(backoff)

            log.info(f"Polling {day} ...")

            try:
                slots = find_slots(day)
            except requests.RequestException as exc:
                log.error(f"  Network error: {exc}")
                backoff = min((backoff or 15) * 2, 300)
                continue

            if slots is None:
                backoff = min((backoff or 30) * 2, 300)
                continue

            backoff = 0
            good = qualifying_slots(slots)

            if not good:
                log.info(f"  No slots at {PREFERRED_START_HOUR}:00+ on {day}")
                time.sleep(1)
                continue

            log.info(f"  {len(good)} qualifying slot(s) on {day} — attempting to book!")

            for slot in good:
                slot_time = slot.get("date", {}).get("start", "unknown")

                # ── Hard date guard ───────────────────────────────────────────
                slot_day = slot_time[:10]
                if not in_target_range(slot_day):
                    log.warning(
                        f"  GUARD: slot date {slot_day} is outside allowed range "
                        f"({START_DATE} to {END_DATE}) — skipping, will not book."
                    )
                    continue

                log.info(f"  Slot: {slot_time} [date guard passed]")

                try:
                    token = get_book_token(slot, day)
                except requests.RequestException as exc:
                    log.error(f"  /3/details network error: {exc}")
                    continue

                if token is None:
                    log.warning("  Could not obtain book_token — skipping slot")
                    continue

                log.info("  Got book_token — booking now ...")

                try:
                    result = book_slot(token)
                except requests.RequestException as exc:
                    log.error(f"  /3/book network error: {exc}")
                    continue

                if result is None:
                    log.warning("  Booking request failed — trying next slot")
                    continue

                if result.get("race"):
                    log.warning(f"  Race condition on {slot_time} — slot gone, keep polling.")
                    continue

                # ── SUCCESS ───────────────────────────────────────────────────
                conf = (
                    result.get("resy_token")
                    or result.get("reservation_id")
                    or result.get("confirmation")
                    or "(check Resy app)"
                )
                print()
                print("=" * 60)
                print("  BOOKED [OK]")
                print("=" * 60)
                print(f"  Venue:        {VENUE_ID}")
                print(f"  Date:         {day}")
                print(f"  Time:         {slot_time}")
                print(f"  Party size:   {PARTY_SIZE}")
                print(f"  Confirmation: {conf}")
                print("=" * 60)
                print()

                send_telegram(
                    f"<b>Reservation booked!</b>\n"
                    f"Venue ID: {VENUE_ID}\n"
                    f"Date: {day}\n"
                    f"Time: {slot_time}\n"
                    f"Party size: {PARTY_SIZE}\n"
                    f"Confirmation: {conf}"
                )

                log.info("Reservation secured. Exiting.")
                return

            log.info(f"  All slots on {day} exhausted — continuing cycle")
            time.sleep(1)

        log.info(f"Full cycle complete — sleeping {POLL_INTERVAL}s before next round ...")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Interrupted by user.")
    except Exception as exc:
        msg = f"Resy sniper crashed: {type(exc).__name__}: {exc}"
        log.exception(msg)
        send_telegram(f"<b>Sniper crashed</b>\n{msg}")
        sys.exit(1)
