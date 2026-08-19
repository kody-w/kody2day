# Kody2day

> "u need a daily Kody2day so i can keep up." — Howard

A daily digest of what Kody shipped across his **public** GitHub, generated
every morning by a cron in this repo and served on GitHub Pages:

- **Site:** https://kody-w.github.io/kody2day/ (one page per day, newest first)
- **Latest, machine-readable:** https://kody-w.github.io/kody2day/latest.json
- **RSS:** https://kody-w.github.io/kody2day/feed.xml
- **From your RAPP brainstem:** install `@kody-w/kody2day_agent` from
  [RAR](https://kody-w.github.io/RAR/) and ask *"what did Kody ship today?"*

## What's in a day

Every public repo pushed in the window, sorted by how much of it was Kody's own
hands: **commits by Kody** (subject lines, linked) separated from **the fleet**
(bots and automated loops — the rappterverse state loop, registry bots, nightly
CI). New repos are tagged. An optional editor's note (`docs/notes/<date>.md`)
renders at the top of that day.

Nothing private can appear here: the builder reads the public repo listing and
public commits only, with a token that has no private scope in Actions.

## Run it yourself

```bash
python3 kody2day.py build                        # last 24h, dated today (UTC)
python3 kody2day.py build --date 2026-08-18      # a specific day
python3 kody2day.py latest                        # print docs/latest.json
```

Stdlib only. Locally it borrows `gh auth token` if you have `gh`; otherwise it
runs unauthenticated (60 requests/hour, plenty for one day). Point it at anyone
with `KODY2DAY_OWNER=<login>` and `KODY2DAY_HUMANS=<login,login>`.

The workflow (`.github/workflows/daily.yml`) runs at 14:05 UTC, commits
`docs/`, and then *reads the page it just wrote* — a green run with no output
would be a stall, not a success.

MIT.

## Private mode (personal impact ledger — never published)

```bash
KODY2DAY_PRIVATE=1 python3 kody2day.py build
```

Writes to `~/.rapp/kody2day-private/docs/` (override with `KODY2DAY_HOME`),
never into this repo: the same daily pages over **all** of the authenticated
user's own repos, private included, plus `impact.json` / `impact.html` — a rolling
7d / 30d / all-time ledger (commits, active days, streak, per-repo activity, new
repos, busiest day). `@kody-w/kody2day_agent` action=`impact` reads that local
file only. Run it from a launchd job outside `~/Documents` (TCC blocks launch
agents there) — e.g. a clone under `~/.rapp/kody2day-private/code`.
