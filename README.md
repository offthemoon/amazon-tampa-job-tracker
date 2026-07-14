# Amazon Tampa Job Tracker

Watches [hiring.amazon.com](https://hiring.amazon.com/locations/tampa-jobs) for
hourly job postings within 50 miles of Tampa, FL and sends a Discord alert the
moment a new one appears. No dependencies — pure Python standard library.

It talks directly to the same GraphQL API the website uses (guest session
token from `/authorize/api/csrf`, then `searchJobCardsByLocation`), so there is
no browser automation to break.

## Hosted on GitHub Actions

[.github/workflows/track.yml](.github/workflows/track.yml) runs a check every
~10 minutes and commits `state.json` back to the repo so runs remember which
jobs have already been seen. New jobs, removed jobs, and pay changes are
appended to `history.jsonl`.

Setup:
1. Add a repository secret named `DISCORD_WEBHOOK` with your Discord webhook URL
   (repo → Settings → Secrets and variables → Actions → New repository secret).
2. That's it. Trigger a manual run from the Actions tab to test.

## Running locally

```bash
python3 tracker.py                # poll every 5 min, desktop notifications
python3 tracker.py --once         # single check
python3 tracker.py --radius 75    # widen search radius
python3 tracker.py --webhook https://discord.com/api/webhooks/...
```
