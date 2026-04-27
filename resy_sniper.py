#!/usr/bin/env python3
"""Resy reservation sniper — two simultaneous venue targets with threading."""

import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import requests

# ── Constants ─────────────────────────────────────────────────────────────────
API_KEY = "REDACTED_API_KEY"
BASE_URL = "https://api.resy.com"
TELEGRAM_API = "https://api.telegram.org"
EASTERN = ZoneInfo("America/New_York")

DROP_HOUR, DROP_MIN, DROP_SEC = 10, 59, 50  # Eastern — Bungalow opens at 11:00
DROP_DURATION = 180   # seconds of aggressive polling (3 min)
DROP_INTERVAL = 2     # seconds between polls in aggressive mode
CHUNK_SLEEP = 10      # seconds per chunk during interruptible inter-cycle sleep

# ── ENV vars ──────────────────────────────────────────────────────────────────
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

# ── Headers ───────────────────────────────────────────────────────────────────
BASE_HEADERS = {
    "Authorization": f'ResyAPI api_key="{API_KEY}"',
    "X-Resy-Auth-Token": AUTH_TOKEN,
    "X-Resy-Universal-Slot": "1",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Origin": "https://resy.com",
    "Referer": "https://resy.com/cities/new-york-ny/venues/",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}
FORM_HEADERS = {**BASE_HEADERS, "Content-Type": "application/x-www-form-urlencoded"}


# ── Venue config ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class VenueTarget:
    name: str
    venue_id: int
    start_date: date
    end_date: date
    party_size: int
    min_hour: int           # 0 = any time; only book slots at or after this hour
    skip_weekdays: frozenset  # Python .weekday(): 0=Mon, 1=Tue, ..., 6=Sun
    poll_interval: int      # seconds between normal polling cycles
    drop_sniper: bool       # True → hammer at DROP_* time for DROP_DURATION seconds

    def dates(self) -> list[str]:
        result, d = [], self.start_date
        while d <= self.end_date:
            if d.weekday() not in self.skip_weekdays:
                result.append(d.strftime("%Y-%m-%d"))
            d += timedelta(days=1)
        return result

    def in_range(self, day_str: str) -> bool:
        try:
            d = datetime.strptime(day_str[:10], "%Y-%m-%d").date()
            return (self.start_date <= d <= self.end_date
                    and d.weekday() not in self.skip_weekdays)
        except ValueError:
            return False


AMBASSADORS = VenueTarget(
    name="Ambassadors Clubhouse",
    venue_id=94741,
    start_date=date(2026, 5, 14),
    end_date=date(2026, 5, 24),
    party_size=4,
    min_hour=17,
    skip_weekdays=frozenset(),
    poll_interval=60,
    drop_sniper=False,
)

# May 14–20, no Tuesdays. May 19 is the Tuesday in this range (weekday 1).
BUNGALOW = VenueTarget(
    name="Bungalow",
    venue_id=80201,
    start_date=date(2026, 5, 14),
    end_date=date(2026, 5, 20),
    party_size=4,
    min_hour=0,
    skip_weekdays=frozenset({1}),  # 1 = Tuesday; skips May 19
    poll_interval=60,
    drop_sniper=True,
)


# ── Custom exceptions ─────────────────────────────────────────────────────────
class ServerError(Exception):
    """5xx from Resy — not a rate limit, do not trigger backoff."""


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


# ── API calls ─────────────────────────────────────────────────────────────────
def find_slots(day: str, venue_id: int, party_size: int) -> Optional[list]:
    """Returns slot list, None on 429, [] when nothing available."""
    resp = requests.get(
        f"{BASE_URL}/4/find",
        params={
            "lat": "0",
            "long": "0",
            "day": day,
            "party_size": party_size,
            "venue_id": venue_id,
        },
        headers=BASE_HEADERS,
        timeout=15,
    )
    if resp.status_code == 429:
        return None
    if resp.status_code >= 500:
        raise ServerError(f"HTTP {resp.status_code}")
    resp.raise_for_status()
    data = resp.json()
    venues = data.get("results", {}).get("venues", [])
    if not venues:
        return []
    return venues[0].get("slots", [])


def get_book_token(slot: dict, day: str, party_size: int) -> Optional[str]:
    """Returns book_token string, or None on failure/rate-limit."""
    config = slot.get("config", {})
    config_id = config.get("token") or config.get("id")
    resp = requests.post(
        f"{BASE_URL}/3/details",
        json={"config_id": config_id, "day": day, "party_size": party_size},
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
def qualifying_slots(slots: list, min_hour: int) -> list:
    """Slots at or after min_hour, sorted earliest first. min_hour=0 returns all."""
    out = []
    for slot in slots:
        start_str = slot.get("date", {}).get("start", "")
        if not start_str:
            continue
        dt = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(start_str, fmt)
                break
            except ValueError:
                pass
        if dt is None:
            try:
                dt = datetime.fromisoformat(start_str)
            except ValueError:
                log.debug(f"  Could not parse date: {start_str!r}")
                continue
        if min_hour == 0 or dt.hour >= min_hour:
            out.append((dt, slot))
    out.sort(key=lambda x: x[0])
    return [s for _, s in out]


# ── Booking helpers ───────────────────────────────────────────────────────────
def attempt_booking(
    target: VenueTarget, slots: list, day: str, tag: str
) -> Optional[tuple[str, str]]:
    """Try qualifying slots in order. Returns (confirmation, slot_time) or None."""
    good = qualifying_slots(slots, target.min_hour)
    if not good:
        return None

    for slot in good:
        slot_time = slot.get("date", {}).get("start", "unknown")
        slot_day = slot_time[:10]

        if not target.in_range(slot_day):
            log.warning(f"  [{tag}] GUARD: {slot_day} outside allowed range — skipping")
            continue

        log.info(f"  [{tag}] Slot: {slot_time}")

        try:
            token = get_book_token(slot, day, target.party_size)
        except requests.RequestException as exc:
            log.error(f"  [{tag}] /3/details error: {exc}")
            continue

        if token is None:
            log.warning(f"  [{tag}] No book_token — skipping slot")
            continue

        log.info(f"  [{tag}] Got book_token — booking ...")

        try:
            result = book_slot(token)
        except requests.RequestException as exc:
            log.error(f"  [{tag}] /3/book error: {exc}")
            continue

        if result is None:
            log.warning(f"  [{tag}] Booking failed — trying next slot")
            continue

        if result.get("race"):
            log.warning(f"  [{tag}] Race condition on {slot_time} — slot gone")
            continue

        conf = (
            result.get("resy_token")
            or result.get("reservation_id")
            or result.get("confirmation")
            or "(check Resy app)"
        )
        return conf, slot_time

    return None


def announce_booking(target: VenueTarget, day: str, slot_time: str, conf: str) -> None:
    print(
        "\n"
        + "=" * 60 + "\n"
        + f"  BOOKED [OK] — {target.name}\n"
        + "=" * 60 + "\n"
        + f"  Restaurant:   {target.name}\n"
        + f"  Venue ID:     {target.venue_id}\n"
        + f"  Date:         {day}\n"
        + f"  Time:         {slot_time}\n"
        + f"  Party size:   {target.party_size}\n"
        + f"  Confirmation: {conf}\n"
        + "=" * 60 + "\n",
        flush=True,
    )
    send_telegram(
        f"<b>Reservation booked!</b>\n"
        f"Restaurant: {target.name}\n"
        f"Date: {day}\n"
        f"Time: {slot_time}\n"
        f"Party size: {target.party_size}\n"
        f"Confirmation: {conf}"
    )


# ── Drop sniper ───────────────────────────────────────────────────────────────
def _drop_window_bounds(now_et: datetime) -> tuple[float, float]:
    """(seconds_to_start, seconds_to_end) for today's drop window. Negative = past."""
    start = now_et.replace(hour=DROP_HOUR, minute=DROP_MIN, second=DROP_SEC, microsecond=0)
    end = start + timedelta(seconds=DROP_DURATION)
    now_ts = now_et.timestamp()
    return start.timestamp() - now_ts, end.timestamp() - now_ts


def run_drop_sniper(target: VenueTarget, dates: list[str], budget: float) -> bool:
    """Poll all dates at DROP_INTERVAL for up to budget seconds.
    Returns True (and sends announcement) if a booking succeeds."""
    tag = target.name
    end_time = time.monotonic() + min(budget, DROP_DURATION)
    log.info(
        f"[{tag}] DROP SNIPER ACTIVE — {DROP_INTERVAL}s intervals, "
        f"{budget:.0f}s remaining in window"
    )
    send_telegram(
        f"<b>[{tag}] Drop sniper activated</b>\n"
        f"Hammering every {DROP_INTERVAL}s until 11:03 EST"
    )

    while time.monotonic() < end_time:
        for day in dates:
            try:
                slots = find_slots(day, target.venue_id, target.party_size)
            except (ServerError, requests.RequestException) as exc:
                log.warning(f"[{tag}] Drop error on {day}: {exc}")
                continue

            if not slots:
                continue

            result = attempt_booking(target, slots, day, tag)
            if result:
                conf, slot_time = result
                announce_booking(target, day, slot_time, conf)
                return True

        remaining = end_time - time.monotonic()
        if remaining > 0:
            time.sleep(min(DROP_INTERVAL, remaining))

    log.info(f"[{tag}] Drop window closed — resuming normal polling")
    return False


# ── Per-venue polling loop ────────────────────────────────────────────────────
def run_target(target: VenueTarget) -> None:
    """Runs until a reservation is booked. Designed for its own daemon thread."""
    tag = target.name
    dates = target.dates()
    backoff = 0

    log.info(
        f"[{tag}] Starting — venue {target.venue_id}, party {target.party_size}, "
        f"{len(dates)} dates"
        + (f", drop-sniper at {DROP_HOUR:02d}:{DROP_MIN:02d}:{DROP_SEC:02d} ET"
           if target.drop_sniper else "")
    )

    while True:
        # ── Drop sniper gate (Bungalow only) ──────────────────────────────────
        if target.drop_sniper:
            now_et = datetime.now(EASTERN)
            to_start, to_end = _drop_window_bounds(now_et)

            if to_end > 0 and to_start <= 0:
                # Inside the window
                if run_drop_sniper(target, dates, to_end):
                    return
                time.sleep(5)
                continue

            if 0 < to_start <= target.poll_interval:
                # Window opens within this cycle — sleep until it starts
                log.info(f"[{tag}] Drop window in {to_start:.0f}s — waiting")
                time.sleep(max(0.5, to_start - 0.5))
                continue

        # ── Normal polling cycle ──────────────────────────────────────────────
        entered_drop = False
        for day in dates:
            # Mid-cycle drop window check so we don't miss the open by a full cycle
            if target.drop_sniper:
                now_et = datetime.now(EASTERN)
                to_start, to_end = _drop_window_bounds(now_et)
                if to_end > 0 and to_start <= 0:
                    entered_drop = True
                    break

            if backoff:
                log.warning(f"[{tag}] Rate-limited — sleeping {backoff}s")
                time.sleep(backoff)

            log.info(f"[{tag}] Polling {day} ...")

            try:
                slots = find_slots(day, target.venue_id, target.party_size)
            except ServerError as exc:
                log.warning(f"[{tag}] Server error ({exc}) — sleeping 60s")
                time.sleep(60)
                continue
            except requests.RequestException as exc:
                log.error(f"[{tag}] Network error: {exc}")
                backoff = min((backoff or 15) * 2, 300)
                continue

            if slots is None:
                backoff = min((backoff or 30) * 2, 300)
                continue

            backoff = 0

            if not slots:
                log.info(f"[{tag}] No slots on {day}")
                time.sleep(1)
                continue

            log.info(f"[{tag}] {len(slots)} slot(s) on {day} — attempting!")
            result = attempt_booking(target, slots, day, tag)
            if result:
                conf, slot_time = result
                announce_booking(target, day, slot_time, conf)
                return

            log.info(f"[{tag}] All slots on {day} exhausted")
            time.sleep(1)

        if entered_drop:
            continue  # Jump back to top to enter drop gate immediately

        # ── Inter-cycle sleep — chunked for Bungalow so drop gate fires fast ─
        log.info(f"[{tag}] Cycle complete — sleeping {target.poll_interval}s")
        if target.drop_sniper:
            end_sleep = time.monotonic() + target.poll_interval
            while time.monotonic() < end_sleep:
                remaining = end_sleep - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(CHUNK_SLEEP, remaining))
                now_et = datetime.now(EASTERN)
                to_start, to_end = _drop_window_bounds(now_et)
                if to_end > 0 and to_start <= 0:
                    break  # Drop window opened — exit sleep early
        else:
            time.sleep(target.poll_interval)


# ── Thread wrapper ────────────────────────────────────────────────────────────
def _thread_wrapper(target: VenueTarget) -> None:
    try:
        run_target(target)
    except Exception as exc:
        msg = f"[{target.name}] Thread crashed: {type(exc).__name__}: {exc}"
        log.exception(msg)
        send_telegram(f"<b>Sniper thread crashed</b>\n{msg}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    targets = [AMBASSADORS, BUNGALOW]

    log.info("Resy sniper starting — 2 targets in parallel")
    for t in targets:
        ds = t.dates()
        log.info(
            f"  {t.name}: venue {t.venue_id}, party {t.party_size}, "
            f"{len(ds)} dates ({ds[0]} → {ds[-1]})"
            + (", drop-sniper" if t.drop_sniper else "")
        )

    threads = [
        threading.Thread(target=_thread_wrapper, args=(t,), name=t.name, daemon=True)
        for t in targets
    ]
    for thread in threads:
        thread.start()

    try:
        while any(t.is_alive() for t in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Interrupted — shutting down")


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
