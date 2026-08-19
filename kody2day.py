#!/usr/bin/env python3
"""kody2day — a daily digest of what Kody shipped, so people can keep up.

Reads the PUBLIC GitHub estate of one owner (default kody-w) — nothing private
can appear here because the API used never sees private repos — and writes one
page per day under docs/daily/, plus docs/latest.json, docs/feed.xml and
docs/index.html for GitHub Pages. Stdlib only. Runs from a daily GitHub Actions
cron with GITHUB_TOKEN; locally it borrows `gh auth token` if present.

    python3 kody2day.py build                 # last 24h, dated today (UTC)
    python3 kody2day.py build --date 2026-08-18 --hours 24
    python3 kody2day.py latest                # print docs/latest.json
    KODY2DAY_PRIVATE=1 python3 kody2day.py build   # PERSONAL: private repos + impact
                                              # ledger, written ONLY to ~/.rapp/kody2day-private

Human commits (Kody's own logins/name) are separated from the fleet (bots and
automated actors), because "Kody pushed 3 commits" and "the rappterverse state
loop applied 400 PRs" are different news. Editor's notes: drop a markdown file
at docs/notes/<date>.md and it is rendered at the top of that day's page.
"""

import argparse
import html
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PRIVATE = os.environ.get("KODY2DAY_PRIVATE", "") == "1"
# Private mode NEVER writes into the repo: it lands in a local-only home (default
# ~/.rapp/kody2day-private) and may include private repos + the impact ledger.
DOCS = (Path(os.environ.get("KODY2DAY_HOME", "") or (Path.home() / ".rapp" / "kody2day-private")).expanduser() / "docs"
        if PRIVATE else ROOT / "docs")
OWNER = os.environ.get("KODY2DAY_OWNER", "kody-w")
SITE = os.environ.get("KODY2DAY_SITE", "https://%s.github.io/kody2day" % OWNER)
HUMAN_LOGINS = {s.strip() for s in os.environ.get("KODY2DAY_HUMANS", "kody-w,rappter1").split(",") if s.strip()}
HUMAN_NAMES = {s.strip() for s in os.environ.get("KODY2DAY_HUMAN_NAMES", "Kody Wildfeuer").split(",") if s.strip()}
BOT_RE = re.compile(r"\[bot\]$|(^|-)bot$|^rapp-bot$|actions|dependabot", re.I)
API = "https://api.github.com"
MAX_PER_REPO = 8


# ── GitHub ───────────────────────────────────────────────────────────────
def _token():
    for k in ("KODY2DAY_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(k):
            return os.environ[k]
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10,
                           stdin=subprocess.DEVNULL)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return ""


def api(path, params=None):
    url = API + path
    if params:
        url += "?" + "&".join("%s=%s" % (k, urllib.request.quote(str(v), safe="")) for k, v in params.items())
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "kody2day/1.0",
                                               "X-GitHub-Api-Version": "2022-11-28"})
    tok = _token()
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8")), resp.headers
    except urllib.error.HTTPError as e:
        if e.code in (404, 409):  # empty repo / gone
            return [], {}
        raise


def paged(path, params=None, stop=None):
    params = dict(params or {}, per_page=100)
    out = []
    page = 1
    while True:
        params["page"] = page
        data, headers = api(path, params)
        if not isinstance(data, list) or not data:
            break
        out.extend(data)
        if stop and stop(data):
            break
        if 'rel="next"' not in (headers.get("Link") or ""):
            break
        page += 1
    return out


def is_human(commit):
    login = ((commit.get("author") or {}).get("login") or "")
    name = ((commit.get("commit") or {}).get("author") or {}).get("name") or ""
    if login in HUMAN_LOGINS or name in HUMAN_NAMES:
        return not BOT_RE.search(login) or login in HUMAN_LOGINS
    return False


def collect(since, until):
    """Everything public the owner pushed between since and until (aware UTC datetimes)."""
    if PRIVATE:  # the authenticated user's own repos, private included — local-only output
        repos = paged("/user/repos", {"affiliation": "owner", "sort": "pushed", "direction": "desc"},
                      stop=lambda page: _parse(page[-1]["pushed_at"]) < since)
    else:
        repos = paged("/users/%s/repos" % OWNER, {"type": "owner", "sort": "pushed", "direction": "desc"},
                      stop=lambda page: _parse(page[-1]["pushed_at"]) < since)
    touched = []
    for r in repos:
        if r.get("fork") or (r.get("private") and not PRIVATE):
            continue
        pushed = _parse(r["pushed_at"])
        created = _parse(r["created_at"])
        if pushed < since:
            continue
        commits = paged("/repos/%s/commits" % r["full_name"],
                        {"since": since.strftime("%Y-%m-%dT%H:%M:%SZ"), "until": until.strftime("%Y-%m-%dT%H:%M:%SZ")})
        human, fleet = [], []
        seen = set()
        for c in commits:
            subject = ((c.get("commit") or {}).get("message") or "").split("\n", 1)[0].strip()
            if not subject or subject in seen:
                continue
            seen.add(subject)
            row = {"sha": c.get("sha", "")[:7], "subject": subject[:160], "url": c.get("html_url", ""),
                   "at": ((c.get("commit") or {}).get("author") or {}).get("date", ""),
                   "by": ((c.get("author") or {}).get("login") or ((c.get("commit") or {}).get("author") or {}).get("name") or "?")}
            (human if is_human(c) else fleet).append(row)
        if not human and not fleet:
            continue
        touched.append({
            "repo": r["name"], "url": r["html_url"], "description": (r.get("description") or "")[:200],
            "stars": r.get("stargazers_count", 0), "language": r.get("language") or "", "private": bool(r.get("private")),
            "homepage": r.get("homepage") or "", "new": since <= created <= until,
            "human": human, "fleet": fleet, "fleet_count": len(fleet), "human_count": len(human),
        })
    touched.sort(key=lambda t: (-t["human_count"], -t["fleet_count"], t["repo"]))
    return touched


def _parse(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# ── digest ───────────────────────────────────────────────────────────────
def build_digest(date, hours):
    until = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    since = until - timedelta(hours=hours)
    now = datetime.now(timezone.utc)
    partial = until > now
    touched = collect(since, min(until, now))
    note_path = DOCS / "notes" / ("%s.md" % date)
    note = note_path.read_text().strip() if note_path.exists() else ""
    return {
        "schema": "kody2day/1.0", "owner": OWNER, "date": date, "hours": hours,
        "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"), "until": until.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "partial": partial,
        "totals": {"repos": len(touched), "human_commits": sum(t["human_count"] for t in touched),
                   "fleet_commits": sum(t["fleet_count"] for t in touched),
                   "new_repos": [t["repo"] for t in touched if t["new"]]},
        "note": note,
        "repos": touched,
        "page": "%s/daily/%s.html" % (SITE, date),
    }


def headline(d):
    t = d["totals"]
    parts = ["%d repo%s touched" % (t["repos"], "" if t["repos"] == 1 else "s"),
             "%d commit%s by Kody" % (t["human_commits"], "" if t["human_commits"] == 1 else "s"),
             "%d by the fleet" % t["fleet_commits"]]
    if t["new_repos"]:
        parts.append("new: " + ", ".join(t["new_repos"][:3]))
    line = " · ".join(parts)
    if d.get("partial"):
        line += " (so far — the day is still running; rebuilt tomorrow)"
    return line


# ── render ───────────────────────────────────────────────────────────────
CSS = """
:root{--bg:#0b0d12;--fg:#e8eaf0;--mut:#98a0b3;--card:#141824;--acc:#7c9cff;--line:#232a3a}
@media(prefers-color-scheme:light){:root{--bg:#f7f8fb;--fg:#14171f;--mut:#5b6478;--card:#fff;--acc:#2f5bff;--line:#e3e6ee}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 -apple-system,Segoe UI,Inter,system-ui,sans-serif}
main{max-width:820px;margin:0 auto;padding:32px 20px 80px}h1{font-size:28px;margin:0 0 4px}h1 a{color:inherit;text-decoration:none}
.sub{color:var(--mut);margin:0 0 20px}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin:0 0 14px}
.card h2{margin:0 0 4px;font-size:18px}.card h2 a{color:var(--acc);text-decoration:none}.desc{color:var(--mut);font-size:14px;margin:0 0 8px}
ul{margin:6px 0 0;padding-left:18px}li{margin:3px 0}li a{color:inherit;text-decoration:none;border-bottom:1px dotted var(--mut)}
.sha{color:var(--mut);font:12px ui-monospace,Menlo,monospace;margin-right:6px}.fleet{color:var(--mut);font-size:14px}
.note{border-left:4px solid var(--acc);padding:10px 14px;background:var(--card);border-radius:8px;margin:0 0 20px;white-space:pre-wrap}
.tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:99px;background:var(--acc);color:#fff;margin-left:6px;vertical-align:middle}
nav{display:flex;gap:14px;flex-wrap:wrap;color:var(--mut);font-size:14px;margin:0 0 22px}nav a{color:var(--acc);text-decoration:none}
.days{list-style:none;padding:0}.days li{padding:8px 0;border-bottom:1px solid var(--line)}.days a{border:0;color:var(--acc)}
footer{color:var(--mut);font-size:13px;margin-top:40px}
"""


def _esc(s):
    return html.escape(str(s or ""), quote=True)


def render_day(d):
    parts = ["<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>",
             "<title>Kody2day — %s</title><style>%s</style><main>" % (_esc(d["date"]), CSS),
             "<h1><a href='../'>Kody2day</a> <span class=tag>%s</span>%s</h1>" % (_esc(d["date"]), " <span class=tag style='background:#c33'>PRIVATE · local only</span>" if PRIVATE else ""),
             "<p class=sub>%s — the last %dh of Kody's public GitHub, generated %s UTC.</p>" % (
                 _esc(headline(d)), d["hours"], _esc(d["generated"][:16].replace("T", " "))),
             "<nav><a href='../'>all days</a><a href='../latest.json'>latest.json</a><a href='../feed.xml'>RSS</a>"
             "<a href='https://github.com/%s'>github.com/%s</a></nav>" % (_esc(OWNER), _esc(OWNER))]
    if d.get("note"):
        parts.append("<div class=note>%s</div>" % _esc(d["note"]))
    if not d["repos"]:
        parts.append("<div class=card><h2>Quiet day</h2><p class=desc>No public pushes in this window.</p></div>")
    for t in d["repos"]:
        parts.append("<div class=card><h2><a href='%s'>%s</a>%s</h2>" % (
            _esc(t["url"]), _esc(t["repo"]), " <span class=tag>new repo</span>" if t["new"] else ""))
        if t["description"]:
            parts.append("<p class=desc>%s</p>" % _esc(t["description"]))
        if t["human"]:
            parts.append("<ul>")
            for c in t["human"][:MAX_PER_REPO]:
                parts.append("<li><span class=sha>%s</span><a href='%s'>%s</a></li>" % (
                    _esc(c["sha"]), _esc(c["url"]), _esc(c["subject"])))
            if len(t["human"]) > MAX_PER_REPO:
                parts.append("<li class=fleet>… and %d more</li>" % (len(t["human"]) - MAX_PER_REPO))
            parts.append("</ul>")
        if t["fleet_count"]:
            sample = "; ".join(_esc(c["subject"])[:70] for c in t["fleet"][:2])
            parts.append("<p class=fleet>fleet: %d automated commit%s%s</p>" % (
                t["fleet_count"], "" if t["fleet_count"] == 1 else "s", (" — e.g. " + sample) if sample else ""))
        parts.append("</div>")
    parts.append("<footer>Public repos only, built by <a href='https://github.com/%s/kody2day'>kody2day</a>. "
                 "Ask your RAPP brainstem: install <code>@kody-w/kody2day_agent</code> from RAR.</footer></main></html>" % _esc(OWNER))
    return "\n".join(parts)


def render_index(days):
    latest = days[0] if days else None
    parts = ["<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>",
             "<title>Kody2day</title><link rel=alternate type=application/rss+xml title=Kody2day href=feed.xml><style>%s</style><main>" % CSS,
             "<h1>Kody2day%s</h1><p class=sub>A daily digest" % (" <span class=tag style='background:#c33'>PRIVATE · local only</span>" if PRIVATE else "") + " of what Kody shipped across his public GitHub — so you can keep up.</p>",
             "<nav>%s<a href='latest.json'>latest.json</a>" % ("<a href='impact.html'>impact ledger</a>" if PRIVATE else "") + "<a href='feed.xml'>RSS</a>"
             "<a href='https://github.com/%s'>github.com/%s</a></nav>" % (_esc(OWNER), _esc(OWNER))]
    if latest:
        parts.append("<div class=card><h2><a href='daily/%s.html'>Today: %s</a></h2><p class=desc>%s</p>" % (
            _esc(latest["date"]), _esc(latest["date"]), _esc(headline(latest))))
        top = [t for t in latest["repos"] if t["human_count"]][:5]
        if top:
            parts.append("<ul>" + "".join("<li><a href='%s'>%s</a> — %s</li>" % (
                _esc(t["url"]), _esc(t["repo"]), _esc(t["human"][0]["subject"])) for t in top) + "</ul>")
        parts.append("</div>")
    parts.append("<h2>Every day</h2><ul class=days>")
    for d in days:
        parts.append("<li><a href='daily/%s.html'>%s</a> <span class=fleet>— %s</span></li>" % (
            _esc(d["date"]), _esc(d["date"]), _esc(headline(d))))
    parts.append("</ul><footer>Public repos only. Built by <a href='https://github.com/%s/kody2day'>kody2day</a>; "
                 "install <code>@kody-w/kody2day_agent</code> from RAR to read it from your brainstem.</footer></main></html>" % _esc(OWNER))
    return "\n".join(parts)


def render_feed(days):
    items = []
    for d in days[:30]:
        body = "<p>%s</p><ul>%s</ul>" % (_esc(headline(d)), "".join(
            "<li><b>%s</b>: %s</li>" % (_esc(t["repo"]), _esc("; ".join(c["subject"] for c in t["human"][:3]) or
                                                             "%d fleet commits" % t["fleet_count"]))
            for t in d["repos"][:12]))
        items.append("<item><title>Kody2day %s — %s</title><link>%s</link><guid>%s</guid><pubDate>%s</pubDate>"
                     "<description>%s</description></item>" % (
                         _esc(d["date"]), _esc(headline(d)), _esc(d["page"]), _esc(d["page"]),
                         datetime.strptime(d["until"], "%Y-%m-%dT%H:%M:%SZ").strftime("%a, %d %b %Y %H:%M:%S +0000"),
                         _esc(body)))
    return ("<?xml version='1.0' encoding='UTF-8'?><rss version='2.0'><channel><title>Kody2day</title>"
            "<link>%s/</link><description>A daily digest of what Kody shipped across his public GitHub.</description>%s"
            "</channel></rss>" % (_esc(SITE), "".join(items)))


# ── impact ledger (personal tracking, computed from every day on record) ──
def compute_impact(days):
    """Rolling totals over the daily digests: 7d / 30d / all-time, streaks, per-repo."""
    days = sorted(days, key=lambda d: d["date"])  # oldest first
    if not days:
        return {"schema": "kody2day-impact/1.0", "days_on_record": 0}
    by_date = {d["date"]: d for d in days}
    last = datetime.strptime(days[-1]["date"], "%Y-%m-%d")

    def window(n):
        cut = (last - timedelta(days=n - 1)).strftime("%Y-%m-%d") if n else "0000-00-00"
        sel = [d for d in days if d["date"] >= cut]
        repos = {}
        for d in sel:
            for t in d["repos"]:
                r = repos.setdefault(t["repo"], {"repo": t["repo"], "url": t["url"], "kody_commits": 0,
                                                 "fleet_commits": 0, "days_active": 0, "last": d["date"], "new": False})
                r["kody_commits"] += t["human_count"]
                r["fleet_commits"] += t["fleet_count"]
                r["days_active"] += 1 if t["human_count"] else 0
                r["last"] = max(r["last"], d["date"])
                r["new"] = r["new"] or bool(t.get("new"))
        table = sorted(repos.values(), key=lambda r: (-r["kody_commits"], -r["fleet_commits"], r["repo"]))
        active = [d for d in sel if d["totals"]["human_commits"]]
        busiest = max(sel, key=lambda d: d["totals"]["human_commits"]) if sel else None
        return {"days": len(sel), "active_days": len(active),
                "kody_commits": sum(d["totals"]["human_commits"] for d in sel),
                "fleet_commits": sum(d["totals"]["fleet_commits"] for d in sel),
                "repos_touched": len(table), "repos_with_kody_commits": sum(1 for r in table if r["kody_commits"]),
                "new_repos": sorted({n for d in sel for n in d["totals"].get("new_repos", [])}),
                "busiest_day": {"date": busiest["date"], "kody_commits": busiest["totals"]["human_commits"]} if busiest else None,
                "per_repo": table[:40]}

    # streak: consecutive calendar days ending at the last recorded day with >=1 Kody commit
    streak = 0
    cur = last
    while True:
        d = by_date.get(cur.strftime("%Y-%m-%d"))
        if not d or not d["totals"]["human_commits"]:
            break
        streak += 1
        cur -= timedelta(days=1)
    daily_series = [{"date": d["date"], "kody": d["totals"]["human_commits"], "fleet": d["totals"]["fleet_commits"],
                     "repos": d["totals"]["repos"], "partial": bool(d.get("partial"))} for d in days[-90:]]
    return {"schema": "kody2day-impact/1.0", "owner": OWNER, "as_of": days[-1]["date"],
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "days_on_record": len(days), "first_day": days[0]["date"], "streak_days": streak,
            "last_7d": window(7), "last_30d": window(30), "all_time": window(0),
            "series": daily_series, "page": SITE + "/impact.html"}


def render_impact(imp):
    def block(title, w):
        if not w:
            return ""
        rows = "".join("<tr><td><a href='%s'>%s</a>%s</td><td>%d</td><td>%d</td><td>%d</td><td>%s</td></tr>" % (
            _esc(r["url"]), _esc(r["repo"]), " <span class=tag>new</span>" if r["new"] else "",
            r["kody_commits"], r["fleet_commits"], r["days_active"], _esc(r["last"])) for r in w["per_repo"][:20])
        busiest = w["busiest_day"]
        return ("<div class=card><h2>%s</h2><p class=desc>%d day%s on record, %d active · <b>%d</b> commits by Kody · %d by the fleet · "
                "%d repos touched (%d with Kody's commits)%s%s</p>"
                "<div style='overflow-x:auto'><table><tr><th>repo</th><th>Kody</th><th>fleet</th><th>days</th><th>last</th></tr>%s</table></div></div>") % (
            _esc(title), w["days"], "" if w["days"] == 1 else "s", w["active_days"], w["kody_commits"], w["fleet_commits"],
            w["repos_touched"], w["repos_with_kody_commits"],
            (" · busiest %s (%d)" % (busiest["date"], busiest["kody_commits"])) if busiest else "",
            (" · new: " + ", ".join(w["new_repos"][:6])) if w["new_repos"] else "", rows)
    bars = ""
    series = imp.get("series") or []
    if series:
        mx = max(1, max(x["kody"] for x in series))
        bars = "<div class=card><h2>Kody's commits per day</h2><div class=bars>" + "".join(
            "<a class=bar href='daily/%s.html' title='%s: %d by Kody, %d fleet' style='height:%d%%%s'></a>" % (
                _esc(x["date"]), _esc(x["date"]), x["kody"], x["fleet"], max(3, int(100 * x["kody"] / mx)),
                ";opacity:.45" if x["partial"] else "") for x in series) + "</div><p class=desc>%s → %s · faded = day still running</p></div>" % (
            _esc(series[0]["date"]), _esc(series[-1]["date"]))
    extra = ("table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line)}"
             "th{color:var(--mut);font-weight:500}td a{color:var(--acc);text-decoration:none}"
             ".bars{display:flex;align-items:flex-end;gap:2px;height:120px}.bar{flex:1;background:var(--acc);border-radius:2px 2px 0 0;min-width:3px}")
    return "\n".join([
        "<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>",
        "<title>Kody2day — impact</title><style>%s%s</style><main>" % (CSS, extra),
        "<h1><a href='./'>Kody2day</a> <span class=tag>impact</span> <span class=tag style='background:#c33'>PRIVATE · local only</span></h1>",
        "<p class=sub>Rolling ledger over every day on record (%d), as of %s · current streak: <b>%d</b> day%s with commits.</p>" % (
            imp.get("days_on_record", 0), _esc(imp.get("as_of", "")), imp.get("streak_days", 0), "" if imp.get("streak_days") == 1 else "s"),
        "<nav><a href='./'>daily</a><a href='impact.json'>impact.json</a><a href='feed.xml'>RSS</a></nav>",
        bars, block("Last 7 days", imp.get("last_7d")), block("Last 30 days", imp.get("last_30d")), block("All time", imp.get("all_time")),
        "<footer>Public repos only. Ask your brainstem: <code>@kody-w/kody2day_agent</code> action='impact'.</footer></main></html>"])


def load_days():
    days = []
    for p in sorted((DOCS / "daily").glob("*.json"), reverse=True):
        try:
            days.append(json.loads(p.read_text()))
        except Exception:
            continue
    return days


def build(date, hours):
    d = build_digest(date, hours)
    (DOCS / "daily").mkdir(parents=True, exist_ok=True)
    (DOCS / "daily" / ("%s.json" % date)).write_text(json.dumps(d, indent=1) + "\n")
    (DOCS / "daily" / ("%s.html" % date)).write_text(render_day(d))
    days = load_days()
    (DOCS / "latest.json").write_text(json.dumps(days[0], indent=1) + "\n")
    (DOCS / "index.html").write_text(render_index(days))
    (DOCS / "feed.xml").write_text(render_feed(days))
    (DOCS / "archive.json").write_text(json.dumps(
        [{"date": x["date"], "headline": headline(x), "page": x["page"]} for x in days], indent=1) + "\n")
    if PRIVATE:  # the impact ledger is personal: only ever written to the private home
        imp = compute_impact(days)
        (DOCS / "impact.json").write_text(json.dumps(imp, indent=1) + "\n")
        (DOCS / "impact.html").write_text(render_impact(imp))
    (DOCS / ".nojekyll").write_text("")
    return d


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    b = sub.add_parser("build")
    b.add_argument("--date", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    b.add_argument("--hours", type=int, default=24)
    sub.add_parser("latest")
    a = ap.parse_args(argv)
    if a.cmd == "build":
        d = build(a.date, a.hours)
        print(json.dumps({"date": d["date"], "headline": headline(d), "page": d["page"],
                          "repos": [t["repo"] for t in d["repos"]]}, indent=1))
        return 0
    if a.cmd == "latest":
        p = DOCS / "latest.json"
        print(p.read_text() if p.exists() else "{}")
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
