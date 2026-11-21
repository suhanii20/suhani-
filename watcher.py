#!/usr/bin/env python3
"""
RISD Workday class-seat watcher.

Opens your saved Workday course-section pages in a real browser (using a login
you do once by hand), reads the seat numbers / section status, and sends a push
notification to your phone through ntfy (https://ntfy.sh) when a full class
opens up.

Commands:
    python watcher.py login         # open a browser, sign in to Workday once
    python watcher.py test-notify   # send a test push to your phone
    python watcher.py check         # check every class once and print results
    python watcher.py watch         # keep checking forever (every N minutes)
    python watcher.py dump URL      # save a page's text to debug/ for tuning
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
import tomllib
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.toml"
STATE_PATH = HERE / "state.json"
PROFILE_DIR = HERE / "browser-profile"
DEBUG_DIR = HERE / "debug"

# Text that means we got bounced to a sign-in page instead of Workday.
LOGIN_HINTS = re.compile(
    r"(sign in|log in|username|password|duo|okta|shibboleth|single sign-on)",
    re.I,
)
LOGIN_URL_HINTS = ("login", "okta", "sso", "shibboleth", "duosecurity", "saml", "idp")

# Patterns Workday Student commonly uses. Labels and values are usually on
# separate lines in the page text, hence the \s* (which matches newlines).
STATUS_RE = re.compile(
    r"Section\s+Status\s*[:\n]?\s*(Open|Closed|Waitlist(?:ed)?|Full|Cancelled|Canceled)",
    re.I,
)
ENROLLED_CAPACITY_RE = re.compile(
    r"Enrolled\s*/\s*Capacity\s*[:\n]?\s*(\d+)\s*/\s*(\d+)", re.I
)
ENROLLED_RE = re.compile(r"(?:Enrolled|Enrollment)(?:\s+Count)?\s*[:\n]\s*(\d+)", re.I)
CAPACITY_RE = re.compile(
    r"(?:Capacity|Enrollment\s+Capacity|Section\s+Capacity|Max(?:imum)?\s+Enrollment)\s*[:\n]\s*(\d+)",
    re.I,
)
SEATS_RE = re.compile(
    r"(?:Seats\s+Available|Available\s+Seats|Open\s+Seats)\s*[:\n]?\s*(\d+)", re.I
)


# --------------------------------------------------------------------------- #
# Config / state
# --------------------------------------------------------------------------- #

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(
            "No config.toml found. Copy config.example.toml to config.toml and "
            "fill it in."
        )
    with CONFIG_PATH.open("rb") as f:
        cfg = tomllib.load(f)
    cfg.setdefault("notify", {})
    cfg.setdefault("workday", {})
    cfg.setdefault("course", [])
    return cfg


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Notifications (ntfy)
# --------------------------------------------------------------------------- #

def notify(cfg: dict, title: str, message: str, url: str | None = None,
           priority: str = "high", tags: str = "rotating_light") -> None:
    n = cfg["notify"]
    topic = n.get("ntfy_topic", "").strip()
    if not topic or topic == "CHANGE-ME":
        log(f"(no ntfy_topic set, would have sent) {title}: {message}")
        return
    server = n.get("ntfy_server", "https://ntfy.sh").rstrip("/")
    headers = {
        "Title": title.encode("utf-8").decode("latin-1", "ignore"),
        "Priority": priority,
        "Tags": tags,
    }
    if url:
        headers["Click"] = url
    if n.get("ntfy_token"):
        headers["Authorization"] = f"Bearer {n['ntfy_token']}"
    req = urllib.request.Request(
        f"{server}/{topic}", data=message.encode("utf-8"), headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
        log(f"Sent notification: {title}")
    except Exception as e:  # noqa: BLE001
        log(f"!! Could not send notification: {e}")


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

@dataclass
class SeatInfo:
    status: str | None = None
    enrolled: int | None = None
    capacity: int | None = None
    seats: int | None = None

    @property
    def is_open(self) -> bool | None:
        """True = seat available, False = full, None = couldn't tell."""
        if self.seats is not None:
            return self.seats > 0
        if self.enrolled is not None and self.capacity is not None:
            return self.enrolled < self.capacity
        if self.status:
            return self.status.lower() == "open"
        return None

    def describe(self) -> str:
        parts = []
        if self.status:
            parts.append(f"status={self.status}")
        if self.enrolled is not None and self.capacity is not None:
            parts.append(f"{self.enrolled}/{self.capacity} enrolled")
        if self.seats is not None:
            parts.append(f"{self.seats} seats available")
        return ", ".join(parts) or "no seat info found"


def parse_seats(text: str, course: dict) -> SeatInfo:
    info = SeatInfo()

    # Optional per-course custom patterns win if given.
    if course.get("seats_pattern"):
        m = re.search(course["seats_pattern"], text, re.I | re.S)
        if m:
            info.seats = int(m.group(1))
    if course.get("enrolled_capacity_pattern"):
        m = re.search(course["enrolled_capacity_pattern"], text, re.I | re.S)
        if m:
            info.enrolled, info.capacity = int(m.group(1)), int(m.group(2))

    if info.seats is None and (m := SEATS_RE.search(text)):
        info.seats = int(m.group(1))
    if info.enrolled is None:
        if m := ENROLLED_CAPACITY_RE.search(text):
            info.enrolled, info.capacity = int(m.group(1)), int(m.group(2))
        else:
            e, c = ENROLLED_RE.search(text), CAPACITY_RE.search(text)
            if e and c:
                info.enrolled, info.capacity = int(e.group(1)), int(c.group(1))
    if m := STATUS_RE.search(text):
        info.status = m.group(1).capitalize()
    return info


def looks_like_login(page_url: str, text: str) -> bool:
    u = page_url.lower()
    if "myworkday.com" in u and not any(h in u for h in LOGIN_URL_HINTS):
        # On Workday itself; only a login page if there is barely any content.
        return len(text) < 400 and bool(LOGIN_HINTS.search(text))
    return any(h in u for h in LOGIN_URL_HINTS) or (
        len(text) < 1500 and bool(LOGIN_HINTS.search(text))
    )


# --------------------------------------------------------------------------- #
# Browser
# --------------------------------------------------------------------------- #

def open_browser(p, headless: bool):
    PROFILE_DIR.mkdir(exist_ok=True)
    extra = {}
    if os.environ.get("CHROME_PATH"):  # optional: use a specific Chrome binary
        extra["executable_path"] = os.environ["CHROME_PATH"]
    return p.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        headless=headless,
        viewport={"width": 1400, "height": 1000},
        **extra,
    )


def page_text(page, url: str, wait_seconds: float) -> str:
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except Exception:  # noqa: BLE001 - Workday sometimes never goes idle
        pass
    # Workday renders after its own JS calls finish; give it a moment and
    # wait until some seat-related label appears if we can.
    try:
        page.wait_for_function(
            """() => /Status|Capacity|Enrolled|Seats/i.test(document.body.innerText)""",
            timeout=wait_seconds * 1000,
        )
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(1500)
    return page.inner_text("body")


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def cmd_login(cfg: dict) -> None:
    from playwright.sync_api import sync_playwright

    home = cfg["workday"].get("home_url", "https://wd5.myworkday.com/risd")
    with sync_playwright() as p:
        ctx = open_browser(p, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(home)
        print(
            "\nA browser window opened. Sign in to RISD Workday (approve Duo,\n"
            "and tick 'remember me' / 'trust this browser' if offered).\n"
            "Once you can see your Workday home page, come back here and press "
            "Enter."
        )
        input()
        ctx.close()
    print("Saved. You can now run:  python watcher.py check")


def check_once(cfg: dict, state: dict, ctx) -> None:
    wait_seconds = float(cfg["workday"].get("page_wait_seconds", 20))
    courses = cfg["course"]
    if not courses:
        log("No [[course]] entries in config.toml yet.")
        return

    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    login_problem = False

    for course in courses:
        name, url = course.get("name", course.get("url")), course.get("url")
        if not url:
            log(f"!! {name}: missing url, skipping")
            continue
        try:
            text = page_text(page, url, wait_seconds)
        except Exception as e:  # noqa: BLE001
            log(f"!! {name}: could not load page ({e})")
            continue

        if looks_like_login(page.url, text):
            login_problem = True
            log(f"!! {name}: got a sign-in page - your Workday login expired.")
            break

        info = parse_seats(text, course)
        is_open = info.is_open
        prev = state.get(url, {}).get("open")
        log(f"{name}: {info.describe()} -> "
            f"{'OPEN' if is_open else 'full' if is_open is False else 'UNKNOWN'}")

        if is_open is None:
            DEBUG_DIR.mkdir(exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9]+", "_", name)[:60]
            (DEBUG_DIR / f"{safe}.txt").write_text(text)
            log(f"   couldn't find seat numbers; saved page text to debug/{safe}.txt")
        elif is_open and prev is not True:
            notify(
                cfg,
                title=f"Seat open: {name}",
                message=f"{name} has space! ({info.describe()}) Tap to open Workday and register.",
                url=url,
                priority="urgent",
            )
        elif not is_open and prev is True and cfg["notify"].get("notify_on_close", False):
            notify(cfg, f"Closed again: {name}", info.describe(), url=url,
                   priority="default", tags="no_entry")

        if is_open is not None:
            state[url] = {
                "name": name,
                "open": is_open,
                "detail": info.describe(),
                "checked": datetime.now().isoformat(timespec="seconds"),
            }
        save_state(state)
        time.sleep(random.uniform(2, 5))  # be polite between pages

    if login_problem:
        if not state.get("_login_alert_sent"):
            notify(
                cfg,
                "Class watcher: please log in again",
                "Your RISD Workday session expired. On your computer run:  python watcher.py login",
                priority="high",
                tags="key",
            )
            state["_login_alert_sent"] = True
    else:
        state["_login_alert_sent"] = False
    save_state(state)


def cmd_check(cfg: dict, loop: bool) -> None:
    from playwright.sync_api import sync_playwright

    headless = bool(cfg["workday"].get("headless", True))
    minutes = float(cfg["workday"].get("check_every_minutes", 10))
    state = load_state()
    with sync_playwright() as p:
        ctx = open_browser(p, headless=headless)
        try:
            while True:
                check_once(cfg, state, ctx)
                if not loop:
                    break
                # jitter so requests don't look robotic / land on the same second
                delay = minutes * 60 * random.uniform(0.85, 1.15)
                log(f"Next check in {delay / 60:.1f} min (Ctrl+C to stop)")
                time.sleep(delay)
                cfg = load_config()  # pick up edits to config.toml live
        except KeyboardInterrupt:
            log("Stopped.")
        finally:
            ctx.close()


def cmd_dump(cfg: dict, url: str) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = open_browser(p, headless=bool(cfg["workday"].get("headless", True)))
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        text = page_text(page, url, float(cfg["workday"].get("page_wait_seconds", 20)))
        DEBUG_DIR.mkdir(exist_ok=True)
        out = DEBUG_DIR / "dump.txt"
        out.write_text(text)
        page.screenshot(path=str(DEBUG_DIR / "dump.png"), full_page=True)
        ctx.close()
    info = parse_seats(text, {})
    print(f"Saved {out} and debug/dump.png")
    print(f"Parsed: {info.describe()}  (open={info.is_open})")


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help", "help"}:
        print(__doc__)
        return
    cmd = sys.argv[1]
    cfg = load_config()
    if cmd == "login":
        cmd_login(cfg)
    elif cmd == "test-notify":
        notify(cfg, "Class watcher test", "If you see this, notifications work!",
               priority="default", tags="white_check_mark")
    elif cmd == "check":
        cmd_check(cfg, loop=False)
    elif cmd == "watch":
        cmd_check(cfg, loop=True)
    elif cmd == "dump" and len(sys.argv) > 2:
        cmd_dump(cfg, sys.argv[2])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
