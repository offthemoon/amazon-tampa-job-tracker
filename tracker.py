#!/usr/bin/env python3
"""
Amazon hiring.amazon.com job tracker — Tampa, FL (or any location).

Talks directly to the same GraphQL API the website uses. No browser, no
Playwright, no dependencies — pure standard library.

How it works:
  1. GET  https://hiring.amazon.com/authorize/api/csrf?countryCode=US
     -> returns a guest session JWT (valid ~1 hour, auto-refreshed here)
  2. POST https://hiring.amazon.com/graphql
     with header  authorization: Bearer Status|unauthenticated|Session <jwt>
     querying searchJobCardsByLocation with a lat/lng + radius filter.

Alerts on NEW jobs, REMOVED jobs, and PAY CHANGES via:
  - console output
  - macOS desktop notifications (on by default, --no-notify to disable)
  - Discord webhook (set DISCORD_WEBHOOK below or --webhook URL)

Usage:
  python3 tracker.py              # poll every 5 minutes, forever
  python3 tracker.py --once       # single check, then exit
  python3 tracker.py --interval 120 --radius 30
"""

import argparse
import json
import os
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# ----------------------------- defaults ------------------------------------
TAMPA_LAT = 27.9506
TAMPA_LNG = -82.4572
DEFAULT_RADIUS_MI = 50
DEFAULT_INTERVAL_S = 300
# Discord webhook: set the DISCORD_WEBHOOK env var (e.g. a GitHub Actions
# secret) or pass --webhook URL on the command line.
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")

BASE = "https://hiring.amazon.com"
CSRF_URL = f"{BASE}/authorize/api/csrf?countryCode=US"
GRAPHQL_URL = f"{BASE}/graphql"
STATE_FILE = Path(__file__).parent / "state.json"
HISTORY_FILE = Path(__file__).parent / "history.jsonl"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
PAGE_SIZE = 100

SEARCH_QUERY = """
query searchJobCardsByLocation($searchJobRequest: SearchJobRequest!) {
  searchJobCardsByLocation(searchJobRequest: $searchJobRequest) {
    nextToken
    jobCards {
      jobId
      jobTitle
      jobType
      employmentType
      city
      state
      postalCode
      locationName
      totalPayRateMin
      totalPayRateMax
      surgePay
      bonusPay
      scheduleCount
      currencyCode
      featuredJob
      distance
    }
  }
}
""".strip()


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ----------------------------- API client ----------------------------------
class AmazonJobsClient:
    """Minimal client for the hiring.amazon.com public GraphQL API."""

    def __init__(self) -> None:
        self._token: str | None = None
        self._token_born: float = 0.0

    def _fetch_token(self) -> str:
        req = urllib.request.Request(
            CSRF_URL,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        token = data.get("token")
        if not token:
            raise RuntimeError(f"csrf endpoint returned no token: {data}")
        return token

    def _token_fresh(self) -> str:
        # Tokens live ~1 hour; refresh after 45 min to stay safe.
        if self._token is None or time.time() - self._token_born > 45 * 60:
            self._token = self._fetch_token()
            self._token_born = time.time()
        return self._token

    def _graphql(self, payload: dict) -> dict:
        body = json.dumps(payload).encode()
        for attempt in (1, 2):
            token = self._token_fresh()
            req = urllib.request.Request(
                GRAPHQL_URL,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                    "Origin": BASE,
                    "Referer": f"{BASE}/locations/tampa-jobs",
                    "authorization": f"Bearer Status|unauthenticated|Session {token}",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    result = json.loads(resp.read())
            except urllib.error.HTTPError as e:
                if e.code in (401, 403) and attempt == 1:
                    self._token = None  # force refresh and retry once
                    continue
                raise
            if result.get("errors"):
                kinds = {e.get("errorType", "?") for e in result["errors"]}
                if kinds & {"UnauthorizedException"} and attempt == 1:
                    self._token = None
                    continue
                raise RuntimeError(f"GraphQL errors: {result['errors']}")
            return result
        raise RuntimeError("unreachable")

    def search_jobs(self, lat: float, lng: float, radius_mi: int) -> list[dict]:
        """All job cards within radius_mi of (lat, lng), following pagination."""
        cards: list[dict] = []
        next_token: str | None = None
        while True:
            request: dict = {
                "locale": "en-US",
                "country": "United States",
                "keyWords": "",
                "equalFilters": [],
                "containFilters": [{"key": "isPrivateSchedule", "val": ["false"]}],
                "rangeFilters": [],
                "orFilters": [],
                "dateFilters": [],
                "sorters": [{"fieldName": "totalPayRateMax", "ascending": "false"}],
                "pageSize": PAGE_SIZE,
                "geoQueryClause": {
                    "lat": lat, "lng": lng, "unit": "mi", "distance": radius_mi,
                },
            }
            if next_token:
                request["nextToken"] = next_token
            result = self._graphql({
                "operationName": "searchJobCardsByLocation",
                "variables": {"searchJobRequest": request},
                "query": SEARCH_QUERY,
            })
            block = result["data"]["searchJobCardsByLocation"]
            cards.extend(block["jobCards"] or [])
            next_token = block.get("nextToken")
            if not next_token or not block["jobCards"]:
                break
        return cards


# ----------------------------- state ---------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            log(f"WARNING: could not parse {STATE_FILE.name}; starting fresh.")
    return {"jobs": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def append_history(event: str, job: dict) -> None:
    entry = {"ts": datetime.now().isoformat(timespec="seconds"),
             "event": event, **job}
    with HISTORY_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def slim(card: dict) -> dict:
    """Keep only the fields we track/compare."""
    return {
        "jobId": card["jobId"],
        "title": card.get("jobTitle") or "(no title)",
        "location": card.get("locationName")
                    or f"{card.get('city', '?')}, {card.get('state', '?')}",
        "jobType": card.get("jobType") or "",
        "employmentType": card.get("employmentType") or "",
        "payMin": card.get("totalPayRateMin"),
        "payMax": card.get("totalPayRateMax"),
        "surgePay": card.get("surgePay") or 0,
        "bonusPay": card.get("bonusPay") or 0,
        "scheduleCount": card.get("scheduleCount") or 0,
        "url": f"{BASE}/app#/jobDetail?jobId={card['jobId']}&locale=en-US",
    }


def pay_str(job: dict) -> str:
    lo, hi = job.get("payMin"), job.get("payMax")
    if lo is None and hi is None:
        return "pay n/a"
    if lo == hi or hi is None:
        s = f"${lo}/hr"
    else:
        s = f"${lo}-${hi}/hr"
    if job.get("surgePay"):
        s += f" +${job['surgePay']} surge"
    if job.get("bonusPay"):
        s += f" +${job['bonusPay']} bonus"
    return s


# ----------------------------- notifications -------------------------------
def notify_desktop(title: str, message: str) -> None:
    if platform.system() != "Darwin":
        return
    script = 'display notification "{}" with title "{}" sound name "Glass"'.format(
        message.replace("\\", "\\\\").replace('"', '\\"'),
        title.replace("\\", "\\\\").replace('"', '\\"'),
    )
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except Exception as e:
        log(f"Desktop notification failed: {e}")


def notify_discord(webhook: str, content: str) -> None:
    if not webhook:
        return
    data = json.dumps({"content": content[:1900]}).encode()
    req = urllib.request.Request(
        webhook, data=data, headers={"Content-Type": "application/json",
                                     "User-Agent": USER_AGENT})
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        log(f"Discord notification failed: {e}")


# ----------------------------- diffing -------------------------------------
def check_once(client: AmazonJobsClient, state: dict, args) -> dict:
    cards = client.search_jobs(args.lat, args.lng, args.radius)
    current = {c["jobId"]: slim(c) for c in cards}
    previous: dict = state.get("jobs", {})
    first_run = not previous and not state.get("baseline_done")

    added = [j for jid, j in current.items() if jid not in previous]
    removed = [j for jid, j in previous.items() if jid not in current]
    changed = []
    for jid, job in current.items():
        old = previous.get(jid)
        if old and (old.get("payMin"), old.get("payMax")) != (job["payMin"], job["payMax"]):
            changed.append((old, job))

    log(f"{len(current)} job(s) within {args.radius} mi of "
        f"({args.lat}, {args.lng}); +{len(added)} new, -{len(removed)} removed, "
        f"{len(changed)} pay change(s).")

    if first_run:
        if current:
            log("First run: recording current listings as baseline (no alerts).")
            for j in current.values():
                log(f"  BASELINE: {j['title']} — {j['location']} ({pay_str(j)})")
        else:
            log("First run: no jobs currently listed. You'll be alerted when one appears.")
    else:
        for j in added:
            line = f"{j['title']} — {j['location']} ({pay_str(j)}) [{j['jobType']}]"
            log(f"  NEW: {line}")
            log(f"       {j['url']}")
            append_history("added", j)
            if not args.no_notify:
                notify_desktop("New Amazon job!", line)
            notify_discord(args.webhook,
                           f"**New Amazon job**\n{line}\n{j['url']}")
        for j in removed:
            log(f"  GONE: {j['title']} — {j['location']}")
            append_history("removed", j)
        for old, new in changed:
            line = (f"{new['title']} — {new['location']}: "
                    f"{pay_str(old)} -> {pay_str(new)}")
            log(f"  PAY CHANGE: {line}")
            append_history("pay_change", new)
            if not args.no_notify:
                notify_desktop("Amazon job pay change", line)
            notify_discord(args.webhook, f"**Pay change**\n{line}\n{new['url']}")

    # Only report state as dirty when something actually changed, so the
    # GitHub Actions run doesn't commit on every check.
    dirty = first_run or bool(added or removed or changed)
    state["jobs"] = current
    state["baseline_done"] = True
    if dirty:
        state["last_check"] = datetime.now().isoformat(timespec="seconds")
    return state, dirty


# ----------------------------- main ----------------------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Track Amazon hourly job postings.")
    p.add_argument("--lat", type=float, default=TAMPA_LAT, help="latitude (default: Tampa)")
    p.add_argument("--lng", type=float, default=TAMPA_LNG, help="longitude (default: Tampa)")
    p.add_argument("--radius", type=int, default=DEFAULT_RADIUS_MI, help="search radius in miles")
    p.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_S, help="seconds between checks")
    p.add_argument("--once", action="store_true", help="check once and exit")
    p.add_argument("--webhook", default=DISCORD_WEBHOOK, help="Discord webhook URL")
    p.add_argument("--no-notify", action="store_true", help="disable macOS desktop notifications")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    client = AmazonJobsClient()
    state = load_state()
    log(f"Amazon job tracker started — {args.radius} mi around "
        f"({args.lat}, {args.lng}), every {args.interval}s. Ctrl+C to stop.")

    consecutive_failures = 0
    while True:
        try:
            state, dirty = check_once(client, state, args)
            if dirty:
                save_state(state)
            consecutive_failures = 0
        except KeyboardInterrupt:
            raise
        except Exception as e:
            consecutive_failures += 1
            log(f"Check failed ({consecutive_failures} in a row): {e}")
            if consecutive_failures >= 5 and not args.no_notify:
                notify_desktop("Amazon job tracker",
                               "5 checks in a row failed — take a look.")
                consecutive_failures = 0
        if args.once:
            break
        # Back off a bit when failing so we don't hammer the API.
        time.sleep(args.interval * (2 if consecutive_failures else 1))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopped.")
