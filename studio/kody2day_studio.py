#!/usr/bin/env python3
"""kody2day_studio — turn each day's Kody2day digest into educational YouTube video.

One episode per day: a long-form 16:9 narrated explainer ("Kody2day <date>") that
teaches ONE RAPP concept using what actually shipped that day as the worked
example, plus N byte-sized 9:16 Shorts that split the long-form into chunks
("Kody2day <date> · byte 1/3"). Two AIs make it, one AI checks it:

  1. digest   read the PUBLIC daily digest (kody-w.github.io/kody2day/daily/<date>.json)
  2. brief    pick the concept (curriculum, never repeats within 14 days) + the day's evidence
  3. write    Claude Code (`claude -p`) writes LONG.json + the Shorts SCRIPT.json files,
              in the rapp-education-shorts contracts; the pack's own lints gate them
  4. refute   GitHub Copilot (`copilot -p`, no tools) is the mandatory REFUTE reviewer:
              every factual claim must trace to the digest; RAPP concept must be right;
              a failing verdict sends the issues back to Claude for ONE revision round
  5. render   kody-w/rapp-education-shorts renders long-form (VibeVoice narration) + Shorts
  6. verify   every MP4 is probed (ffprobe duration, size) — R1: read the artifact
  7. record   episode ledger + YOUTUBE.json (titles/descriptions/chapters) in the queue,
              and one rapp/1 frame on the sentinel's `kody2day` chain if a live
              rapp-sentinel is installed (its neighbor cadence then fails if a morning
              passes without a rendered episode)

Everything lands under ~/.rapp/kody2day-studio/episodes/<date>/. Uploading to
YouTube is the human step: the queue holds the files + metadata ready to drop in.

    python3 kody2day_studio.py run [--date YYYY-MM-DD] [--shorts 3] [--tts vibevoice|none] [--quality draft|high]
    python3 kody2day_studio.py status
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SITE = os.environ.get("KODY2DAY_SITE", "https://kody-w.github.io/kody2day").rstrip("/")
STUDIO = Path(os.environ.get("KODY2DAY_STUDIO", "") or (Path.home() / ".rapp" / "kody2day-studio")).expanduser()
PACK_HOME = Path(os.environ.get("EDUCATION_SHORTS_HOME", "") or (Path.home() / ".rapp" / "education-shorts")).expanduser()
PACK_REPO = "https://github.com/kody-w/rapp-education-shorts"
SENTINEL = Path(os.environ.get("RAPP_SENTINEL_LIVE", "") or (Path.home() / "rapp-sentinel")).expanduser()
CLAUDE_MODEL = os.environ.get("KODY2DAY_CLAUDE_MODEL", "")            # "" = the CLI's default
COPILOT_MODEL = os.environ.get("KODY2DAY_COPILOT_MODEL", "gpt-5.6-sol")

# The curriculum: one concept per episode, chosen against the day's evidence, never
# repeated inside 14 days. Kept plain so a viewer could read it as a syllabus.
CURRICULUM = [
    ("brainstem", "The RAPP brainstem: one local /chat endpoint, every capability is a Python agent you drop in a folder"),
    ("rar", "RAR, the RAPP Agent Registry: publish an agent.py, anyone installs it into their brainstem in one line"),
    ("agent-shape", "The shape of a RAPP agent: manifest, metadata with parameters, perform() — why that is enough"),
    ("rapp1-chains", "rapp/1 frames and hash chains: a record that can't quietly lie, verifiable by anyone"),
    ("sentinel", "The Sentinel pattern: free health checks, a model only on failure, the freedom dial (levels 0-3)"),
    ("r1r2r3", "R1/R2/R3: receipts aren't evidence, ran isn't worked, require known-good — how to watch software honestly"),
    ("neighborhood", "N AIs walk into a bar: a roster of watchers from any vendor keeping mutually-verifiable chains"),
    ("factories", "Factories and eggs: an agent that builds agents, packed as a twin you can hatch anywhere"),
    ("local-first", "Local-first AI: no keys, no cloud in the loop, the whole thing runs on the machine in front of you"),
    ("above-not-beside", "Above AI, not beside it: declare invariants and let agents do the work under them"),
    ("evidence", "Evidence over claims: how every RAPP artifact points at the thing that proves it"),
    ("molting", "Molting: shipping changes to a running organism without killing it (backward-compatible growth paths)"),
    ("proofs", "prove_*.py: break/control pairs that reproduce the old blindness before proving the fix"),
    ("open-estate", "The public estate: how hundreds of small public repos compose into one system"),
]

LONG_EXAMPLE = {
    "schema": "rapp-education-long/1.0",
    "title": "Kody2day 2026-08-18 — a watchdog that can't quietly lie",
    "tagline": "What shipped, and the idea underneath it",
    "chip": "Kody2day",
    "sections": [
        {"kind": "cold_open", "heading": "Nineteen days, every light green", "narration": "…40–95 words…",
         "visual": {"type": "title", "lines": ["The site was up. It had been lying for nineteen days."]}},
        {"kind": "explain", "heading": "What shipped today", "narration": "…", "visual": {"type": "bullets", "items": ["…", "…", "…"]}},
        {"kind": "steps", "heading": "How a check earns trust", "narration": "…", "visual": {"type": "steps", "items": ["…", "…", "…"]}},
        {"kind": "example", "heading": "Ask the brainstem", "narration": "…",
         "visual": {"type": "dialogue", "turns": [{"who": "user", "text": "…"}, {"who": "agent", "text": "…"}]}},
        {"kind": "stat", "heading": "By the numbers", "narration": "…", "visual": {"type": "stat", "value": "35", "caption": "checks, all free"}},
        {"kind": "fit", "heading": "Where it fits", "narration": "…", "visual": {"type": "cards", "items": [{"title": "…", "text": "…"}]}},
        {"kind": "install", "heading": "Try it", "narration": "…", "visual": {"type": "terminal", "lines": ["git clone …", "python3 health.py"]}},
        {"kind": "outro", "heading": "Tomorrow", "narration": "…", "visual": {"type": "title", "lines": ["Kody2day — every morning"]}},
    ],
}
SHORT_EXAMPLE = {
    "schema": "rapp-education-short/1.0",
    "title": "Kody2day 2026-08-18 · byte 1/3",
    "topic": "…", "chip": "Kody2day · byte 1/3",
    "scenes": [
        {"kind": "hook", "heading": "Every light was green", "lines": ["and the site had been frozen for 19 days"], "emphasis": ["green", "frozen"]},
        {"kind": "point", "heading": "…", "lines": ["…", "…"], "emphasis": []},
        {"kind": "steps", "heading": "…", "lines": ["…"], "visual": {"type": "steps", "items": ["…", "…", "…"]}, "emphasis": []},
        {"kind": "compare", "heading": "…", "lines": ["…"], "visual": {"type": "compare", "left": "a claim", "right": "the evidence"}, "emphasis": []},
        {"kind": "number", "heading": "…", "lines": ["…"], "visual": {"type": "number", "value": "35", "caption": "checks, all free"}, "emphasis": []},
        {"kind": "recap", "heading": "…", "lines": ["…", "…"], "emphasis": []},
        {"kind": "cta", "heading": "Kody2day, every morning", "lines": ["full episode on the channel"], "visual": {"type": "cta", "text": "Follow for more"}, "emphasis": []},
    ],
}


# ── plumbing ─────────────────────────────────────────────────────────────
def log(ep, msg):
    line = "[%s] %s" % (datetime.now(timezone.utc).strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    with open(ep / "studio.log", "a") as fh:
        fh.write(line + "\n")


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "kody2day-studio/1.0", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def ensure_pack():
    pack = PACK_HOME / "pack"
    if (pack / "shorts.py").exists():
        subprocess.run(["git", "-C", str(pack), "pull", "-q", "--ff-only"], capture_output=True, timeout=120)
        return pack
    pack.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["git", "clone", "--depth", "1", PACK_REPO, str(pack)], capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not (pack / "shorts.py").exists():
        raise SystemExit("could not clone %s: %s" % (PACK_REPO, r.stderr[-300:]))
    return pack


def extract_json(text):
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("no JSON object in model output")
    return json.loads(text[i:j + 1])


def run_claude(prompt, workdir, timeout=900):
    if not shutil.which("claude"):
        raise SystemExit("claude CLI not on PATH")
    argv = ["claude", "-p", "--output-format", "json", "--tools", "", "--max-turns", "3", "--no-session-persistence"]
    if CLAUDE_MODEL:
        argv += ["--model", CLAUDE_MODEL]
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)  # allow nesting from an interactive session
    p = subprocess.run(argv, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=str(workdir), env=env)
    if p.returncode != 0:
        raise RuntimeError("claude exit %d: %s" % (p.returncode, (p.stderr or p.stdout)[-400:]))
    try:
        doc = json.loads(p.stdout)
        return doc.get("result") or ""
    except Exception:
        return p.stdout


def run_copilot(prompt, workdir, timeout=600):
    exe = shutil.which("copilot")
    if not exe:
        return None, "copilot CLI not on PATH"
    argv = [exe, "-p", prompt, "--model", COPILOT_MODEL, "--available-tools=", "--log-level", "none",
            "--log-dir", str(Path(workdir) / "copilot-logs")]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=str(workdir), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, "copilot timed out"
    if p.returncode != 0:
        return None, "copilot exit %d: %s" % (p.returncode, (p.stderr or "")[-300:])
    return p.stdout, None


# ── stages ───────────────────────────────────────────────────────────────
def stage_digest(ep, date):
    d = fetch_json("%s/daily/%s.json" % (SITE, date))
    (ep / "digest.json").write_text(json.dumps(d, indent=1))
    return d


def recent_concepts(days=14):
    seen = set()
    cut = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    for p in (STUDIO / "episodes").glob("*/episode.json"):
        try:
            e = json.loads(p.read_text())
            if e.get("date", "") >= cut and e.get("concept"):
                seen.add(e["concept"])
        except Exception:
            pass
    return seen


def stage_brief(ep, d, shorts_n):
    avoid = recent_concepts()
    menu = [{"id": k, "concept": v} for k, v in CURRICULUM if k not in avoid] or [{"id": k, "concept": v} for k, v in CURRICULUM]
    evidence = []
    for t in d.get("repos", []):
        if not t["human_count"]:
            continue
        evidence.append({"repo": t["repo"], "url": t["url"], "about": t.get("description", ""), "kody_commits": t["human_count"],
                         "fleet_commits": t["fleet_count"], "new_repo": bool(t.get("new")),
                         "shipped": [c["subject"] for c in t["human"][:10]]})
    fleet = [{"repo": t["repo"], "fleet_commits": t["fleet_count"]} for t in d.get("repos", []) if t["fleet_count"] and not t["human_count"]]
    brief = {"schema": "kody2day-brief/1.0", "date": d["date"],
             "scope": "This digest covers Kody's PUBLIC GitHub repos only; every repo named here is public. 'human_commits' = "
                      "commits by Kody himself; 'fleet_commits' = bots and automated loops. Repos in fleet_only had no Kody commits.",
             "headline_totals": d["totals"], "evidence": evidence[:12],
             "fleet_only": fleet[:10], "concept_menu": menu, "shorts": shorts_n, "note": d.get("note") or ""}
    (ep / "brief.json").write_text(json.dumps(brief, indent=1))
    return brief


def write_prompt(brief, issues=None, previous=None):
    p = ["You are the writer for Kody2day, a daily educational YouTube show that teaches RAPP (Kody Wildfeuer's local-first, "
         "agent-native way of building software: a brainstem you run on your own machine, agents as single Python files, "
         "a public registry, tamper-evident rapp/1 chains, sentinels that watch honestly). Each episode teaches ONE concept "
         "from the menu and uses what ACTUALLY SHIPPED that day (the evidence) as the worked example.",
         "HARD RULES: every factual claim about what shipped must be traceable to the evidence list (repo names, commit "
         "subjects, counts) — never invent commits, features, numbers or quotes. Plain, warm, precise; no hype, no emojis, "
         "no URLs, no @handles, no customer or company names other than the public repos named in the evidence. "
         "Explain the concept so a curious developer who has never heard of RAPP follows it. Say 'Kody' in third person.",
         "Return ONE JSON object and nothing else: {\"concept\": <menu id>, \"long\": <LONG.json>, \"shorts\": [<SCRIPT.json>, ...], "
         "\"youtube\": {\"title\": ..., \"description\": ..., \"tags\": [...], \"chapters\": [{\"section\": <heading>, \"label\": ...}]}}",
         "LONG.json contract (schema rapp-education-long/1.0): title, tagline, chip 'Kody2day', 6-12 sections; the first is kind "
         "cold_open and the last is outro; each section: kind in (cold_open, explain, steps, example, stat, fit, install, outro), "
         "heading (<=42 chars), narration (40-95 words the voice reads; total 300-800 words), visual per kind exactly as in the "
         "example. Section 2 should be 'explain' headed 'What shipped today' with 3-4 bullets drawn from the evidence. Use a "
         "'stat' whose value comes from the evidence totals. The install section shows real commands from the concept "
         "(git clone of a public kody-w repo, python3 ...).",
         "Example LONG.json (shape only): " + json.dumps(LONG_EXAMPLE),
         "SCRIPT.json contract (schema rapp-education-short/1.0): title 'Kody2day <date> · byte i/N', topic, chip, 4-8 scenes; "
         "kinds hook/point/steps/compare/number/quote/recap/cta; heading <=42 chars; each scene up to 3 lines of <=12 words; "
         "emphasis = words that appear in the lines. Visuals: visual.type ALWAYS equals the scene kind; steps → visual.items "
         "(2-5 short strings); compare → visual.left and visual.right are plain STRINGS; number → visual.value (digits, "
         "optional %%/x/K/M/B/+ suffix) + visual.caption; cta → visual.text; hook/point/quote/recap need no visual. Each Short "
         "is ONE self-contained byte of the long-form (byte 1 = the hook + what shipped, later bytes = the concept, the last "
         "byte = try it + recap), watchable with the sound off, under 59 seconds — so keep words few. Write exactly %d Shorts."
         % brief["shorts"],
         "Example SCRIPT.json (shape only): " + json.dumps(SHORT_EXAMPLE),
         "BRIEF: " + json.dumps(brief)]
    if issues:
        p.append("A REVIEWER REFUTED THE PREVIOUS DRAFT. Fix every issue below without introducing new claims. Issues: " +
                 json.dumps(issues) + "  Previous draft: " + json.dumps(previous)[:12000])
    return "\n\n".join(p)


def lint_all(pack, draft, shorts_n):
    sys.path.insert(0, str(pack))
    from eshorts.long import lint_long  # noqa
    from eshorts.script import lint_script  # noqa
    fails = []
    if not isinstance(draft, dict):
        return ["draft is not an object"]
    if not any(draft.get("concept") == k for k, _ in CURRICULUM):
        fails.append("concept %r not in curriculum" % draft.get("concept"))
    try:
        fails += ["long: " + x for x in lint_long(draft.get("long"))]
    except Exception as e:
        fails.append("long: contract violation (%s: %s)" % (type(e).__name__, e))
    shorts = draft.get("shorts")
    if not isinstance(shorts, list) or len(shorts) != shorts_n:
        fails.append("need exactly %d shorts" % shorts_n)
    else:
        for i, s in enumerate(shorts, 1):
            try:
                fails += ["short %d: %s" % (i, x) for x in lint_script(s)]
            except Exception as e:  # the pack's lint assumes well-typed visuals; a crash is a contract violation
                fails.append("short %d: visual shape invalid (%s: %s) — visual.left/right/items/caption must be strings" % (i, type(e).__name__, e))
    yt = draft.get("youtube") or {}
    if not (isinstance(yt, dict) and yt.get("title") and yt.get("description")):
        fails.append("youtube.title/description missing")
    return fails


def stage_write(ep, pack, brief, shorts_n, issues=None, previous=None, attempts=3):
    last = None
    for i in range(1, attempts + 1):
        log(ep, "write: claude attempt %d%s" % (i, " (revision after refute)" if issues else ""))
        raw = run_claude(write_prompt(brief, issues, previous), ep)
        (ep / ("draft-%d%s.txt" % (i, "-rev" if issues else ""))).write_text(raw)
        try:
            draft = extract_json(raw)
        except Exception as e:
            last = ["no JSON: %s" % e]
            continue
        fails = lint_all(pack, draft, shorts_n)
        if not fails:
            return draft
        last = fails
        log(ep, "write: lint failed: %s" % "; ".join(fails)[:400])
        issues = (issues or []) + [{"lint": f} for f in fails]
        previous = draft
    raise RuntimeError("writer never passed lint: %s" % last)


def refute_prompt(brief, draft):
    return ("You are the REFUTE reviewer for Kody2day, an educational video about RAPP. Your job is to find reasons the draft "
            "must NOT ship. Check, in order: (1) FACTS — every claim about what shipped (repos, commit subjects, counts, 'new repo') "
            "traces to the BRIEF evidence; anything not in the evidence is a fabrication; (2) CONCEPT — the RAPP concept is "
            "explained correctly and matches the chosen menu item; (3) SAFETY — no URLs, handles, customer or company names, "
            "no private-sounding paths or secrets; (4) TEACHING — a newcomer could follow it; the Shorts each stand alone. "
            "The BRIEF's 'scope' line is ground truth (e.g. every repo in it is public). Be strict about facts and concept, lenient about style; "
            "a 'high' issue is a fabricated or contradicted fact, a wrong explanation of RAPP, or a safety violation — clarity gaps are 'low'. Return ONE JSON object only: "
            "{\"verdict\": \"pass\"|\"fail\", \"issues\": [{\"where\": \"long section N|short i scene j|youtube\", \"severity\": "
            "\"high\"|\"low\", \"issue\": ..., \"fix\": ...}]}. verdict is fail if any high-severity issue exists.\n\nBRIEF: "
            + json.dumps(brief) + "\n\nDRAFT: " + json.dumps(draft))


def stage_refute(ep, brief, draft):
    out, err = run_copilot(refute_prompt(brief, draft), ep)
    if err:
        log(ep, "refute: copilot unavailable (%s) — recorded, NOT treated as a pass" % err)
        return {"verdict": "unavailable", "issues": [], "error": err}
    (ep / "refute-raw.txt").write_text(out)
    try:
        v = extract_json(out)
    except Exception as e:
        v = {"verdict": "unparseable", "issues": [], "error": str(e)}
    (ep / "refute.json").write_text(json.dumps(v, indent=1))
    return v


def stage_render(ep, pack, date, draft, tts, quality):
    root = ep / "shorts-root"
    root.mkdir(exist_ok=True)
    slug = "kody2day-%s" % date
    (ep / "LONG.json").write_text(json.dumps(draft["long"], indent=1))
    outputs = {}
    base = [sys.executable, str(pack / "shorts.py"), "--root", str(root)]
    log(ep, "render: long-form (%s, %s)" % (tts, quality))
    r = subprocess.run(base + ["long", slug, "--topic", draft["long"]["title"], "--long-script", str(ep / "LONG.json"),
                              "--tts", tts, "--quality", quality], capture_output=True, text=True, timeout=5400, cwd=str(pack),
                       env=dict(os.environ, NO_COLOR="1"))
    (ep / "render-long.log").write_text((r.stdout or "") + "\n--- stderr ---\n" + (r.stderr or ""))
    mp4 = root / slug / "out" / ("%s-long.mp4" % slug)
    outputs["long"] = {"ok": r.returncode == 0 and mp4.exists(), "path": str(mp4), "exit": r.returncode}
    for i, s in enumerate(draft["shorts"], 1):
        sslug = "%s-byte-%d" % (slug, i)
        sp = ep / ("SHORT-%d.json" % i)
        sp.write_text(json.dumps(s, indent=1))
        log(ep, "render: short %d/%d" % (i, len(draft["shorts"])))
        r = subprocess.run(base + ["once", sslug, "--topic", s.get("topic") or s["title"], "--script", str(sp), "--quality", quality],
                           capture_output=True, text=True, timeout=1800, cwd=str(pack), env=dict(os.environ, NO_COLOR="1"))
        (ep / ("render-short-%d.log" % i)).write_text((r.stdout or "") + "\n--- stderr ---\n" + (r.stderr or ""))
        smp4 = root / sslug / "out" / ("%s.mp4" % sslug)
        outputs["short_%d" % i] = {"ok": r.returncode == 0 and smp4.exists(), "path": str(smp4), "exit": r.returncode}
    return outputs


def probe(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration,size", "-of", "json", path],
                           capture_output=True, text=True, timeout=60)
        f = json.loads(r.stdout)["format"]
        return {"seconds": round(float(f["duration"]), 1), "bytes": int(f["size"])}
    except Exception as e:
        return {"error": str(e)}


def stage_verify(ep, outputs):
    verified = {}
    for k, o in outputs.items():
        v = dict(o)
        if o.get("ok"):
            v.update(probe(o["path"]))
            v["ok"] = v.get("seconds", 0) > 5 and v.get("bytes", 0) > 100000
            if k.startswith("short") and v.get("seconds", 0) > 60:
                v["ok"] = False
                v["why"] = "short over 60s"
        verified[k] = v
    return verified


def stage_record(ep, date, draft, refute, verified):
    queue = STUDIO / "queue" / date
    queue.mkdir(parents=True, exist_ok=True)
    delivered = {}
    for k, v in verified.items():
        if v.get("ok"):
            dst = queue / ("kody2day-%s-%s.mp4" % (date, "long" if k == "long" else k.replace("_", "-")))
            shutil.copy2(v["path"], dst)
            delivered[k] = str(dst)
    yt = dict(draft.get("youtube") or {})
    yt["files"] = delivered
    yt["shorts_titles"] = [s.get("title") for s in draft.get("shorts", [])]
    (queue / "YOUTUBE.json").write_text(json.dumps(yt, indent=1))
    all_ok = bool(verified) and all(v.get("ok") for v in verified.values())
    episode = {"schema": "kody2day-episode/1.0", "date": date, "concept": draft.get("concept"),
               "title": (draft.get("long") or {}).get("title"), "refute": refute.get("verdict"),
               "refute_issues": len(refute.get("issues") or []), "outputs": verified, "queue": str(queue),
               "ok": all_ok, "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    (ep / "episode.json").write_text(json.dumps(episode, indent=1))
    with open(STUDIO / "ledger.jsonl", "a") as fh:
        fh.write(json.dumps(episode) + "\n")
    return episode


def emit_frame(kind, payload):
    """One rapp/1 frame on the live sentinel's `kody2day` chain (if installed). Never fatal."""
    if not (SENTINEL / "neighborhood.py").exists():
        return "no live sentinel at %s" % SENTINEL
    code = ("import json,sys; sys.path.insert(0, %r); import neighborhood as n; "
            "print(json.dumps(n.emit('kody2day', %r, json.loads(sys.stdin.read()))['seq']))" % (str(SENTINEL), kind))
    try:
        env = dict(os.environ)
        env.pop("SENTINEL_HOME", None)
        r = subprocess.run(["/usr/bin/python3", "-c", code], input=json.dumps(payload), capture_output=True, text=True,
                           timeout=60, cwd=str(SENTINEL), env=env)
        return "seq %s" % r.stdout.strip() if r.returncode == 0 else "emit failed: %s" % (r.stderr or "")[-200:]
    except Exception as e:
        return "emit failed: %s" % e


# ── driver ───────────────────────────────────────────────────────────────
def run(date, shorts_n, tts, quality, skip_render=False):
    ep = STUDIO / "episodes" / date
    ep.mkdir(parents=True, exist_ok=True)
    log(ep, "episode %s: start" % date)
    emit_frame("studio.run", {"date": date, "stage": "start"})
    pack = ensure_pack()
    d = stage_digest(ep, date)
    if not d.get("totals", {}).get("human_commits"):
        log(ep, "quiet day (no Kody commits) — no episode")
        emit_frame("studio.run", {"date": date, "stage": "skipped", "why": "quiet day"})
        return 0
    brief = stage_brief(ep, d, shorts_n)
    draft = stage_write(ep, pack, brief, shorts_n)
    refute = stage_refute(ep, brief, draft)
    log(ep, "refute: %s (%d issues)" % (refute.get("verdict"), len(refute.get("issues") or [])))
    if refute.get("verdict") == "fail":
        high = [i for i in refute.get("issues") or [] if i.get("severity") == "high"] or refute.get("issues")
        draft = stage_write(ep, pack, brief, shorts_n, issues=high, previous=draft)
        refute2 = stage_refute(ep, brief, draft)
        log(ep, "refute (round 2): %s (%d issues)" % (refute2.get("verdict"), len(refute2.get("issues") or [])))
        if refute2.get("verdict") == "fail":
            (ep / "episode.json").write_text(json.dumps({"date": date, "ok": False, "why": "refuted twice",
                                                          "issues": refute2.get("issues")}, indent=1))
            emit_frame("studio.run", {"date": date, "stage": "refuted", "issues": len(refute2.get("issues") or [])})
            log(ep, "refuted twice — no episode today (drafts kept for a human)")
            return 1
        refute = refute2
    (ep / "draft.json").write_text(json.dumps(draft, indent=1))
    if skip_render:
        log(ep, "skip_render — scripts written, stopping")
        return 0
    outputs = stage_render(ep, pack, date, draft, tts, quality)
    verified = stage_verify(ep, outputs)
    episode = stage_record(ep, date, draft, refute, verified)
    log(ep, "episode %s: %s — %s" % (date, "OK" if episode["ok"] else "INCOMPLETE",
                                     ", ".join("%s %ss" % (k, v.get("seconds")) for k, v in verified.items())))
    log(ep, "sentinel frame: %s" % emit_frame("studio.render", {
        "date": date, "ok": episode["ok"], "concept": episode["concept"],
        "outputs": {k: {"ok": v.get("ok"), "seconds": v.get("seconds")} for k, v in verified.items()}}))
    return 0 if episode["ok"] else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run")
    r.add_argument("--date", default=(datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d"),
                   help="digest day (default: yesterday UTC — the last COMPLETE day)")
    r.add_argument("--shorts", type=int, default=3)
    r.add_argument("--tts", default="vibevoice", choices=["vibevoice", "none"])
    r.add_argument("--quality", default="high", choices=["draft", "high"])
    r.add_argument("--skip-render", action="store_true")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        return run(a.date, a.shorts, a.tts, a.quality, a.skip_render)
    if a.cmd == "status":
        led = STUDIO / "ledger.jsonl"
        rows = [json.loads(x) for x in led.read_text().splitlines() if x.strip()] if led.exists() else []
        print(json.dumps({"studio": str(STUDIO), "episodes": len(rows), "last": rows[-1] if rows else None}, indent=1))
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
