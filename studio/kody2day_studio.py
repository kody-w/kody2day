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
IMESSAGE_TO = os.environ.get("KODY2DAY_IMESSAGE", "").strip()      # phone/handle to text the finished episode to (never in the repo)
IMESSAGE_MAX_MB = float(os.environ.get("KODY2DAY_IMESSAGE_MAX_MB", "95"))

# Tools live in places a launchd job or a server process may not have on PATH.
EXTRA_BIN = [str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin",
             str(Path.home() / "Library" / "Application Support" / "Code" / "User" / "globalStorage" / "github.copilot-chat" / "copilotCli"),
             str(Path.home() / ".copilot" / "bin"), str(Path.home() / ".npm-global" / "bin")]
os.environ["PATH"] = os.pathsep.join([d for d in EXTRA_BIN if Path(d).is_dir()] + [os.environ.get("PATH", "")])

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

# Grounded concept cards: the ONLY RAPP facts the writer may state beyond the day's evidence.
CONCEPT_CARDS = {
    "brainstem": {"facts": ["The brainstem is a small local HTTP server (Flask) on the developer's own machine, port 7071.",
                            "Everything goes through one POST /chat call; the reply carries a 'response' field.",
                            "A capability is one Python file dropped into an agents/ folder: a class with metadata (name, description, parameters) and a perform() method.",
                            "No cloud is required to run it; a model can be plugged in, but the routing and agents are local files you can read."],
                  "install": ["git clone https://github.com/kody-w/RAPP", "python3 -m pip install -r requirements.txt", "curl -X POST localhost:7071/chat -d '{\"user_input\":\"hello\"}'"]},
    "rar": {"facts": ["RAR is the public RAPP Agent Registry: a GitHub repo plus a static site listing every published agent.",
                      "An agent is published as one file, agents/@owner/<name>_agent.py, with a __manifest__ (name, version, description, tags, category).",
                      "Every published file carries a notarized receipt; the registry rebuilds registry.json and a static API on each change.",
                      "Anyone can install a listed agent into their own brainstem; the registry ranks by community upvotes, tier, freshness and depth."],
            "install": ["git clone https://github.com/kody-w/RAR", "python3 build_registry.py"]},
    "agent-shape": {"facts": ["A RAPP agent is a single Python file: a __manifest__ dict, a class extending BasicAgent, metadata with a JSON-schema 'parameters' block, and perform(**kwargs) returning a string (usually JSON).",
                              "The metadata description is what the model reads to decide when to call the agent, so it is written for a reader, not a compiler.",
                              "Stdlib only is the norm; anything the agent needs on the machine is declared as an external prerequisite."],
                    "install": ["git clone https://github.com/kody-w/RAR", "python3 agents/@kody-w/hello_world_agent.py"]},
    "rapp1-chains": {"facts": ["A rapp/1 chain is an append-only JSONL file of frames; each frame carries a kind, a sequence number, a UTC time, a payload, and the hash of the previous frame's payload.",
                               "Because every frame binds the one before it, truncation or rewriting is detectable by anyone who re-reads the file from genesis.",
                               "A chain records that something was written, not that it was true: it catches a stalled or tampered record, never a liar."],
                     "install": ["git clone https://github.com/kody-w/rapp-sentinel", "python3 neighborhood.py roll-call"]},
    "sentinel": {"facts": ["rapp-sentinel is a watchdog for GitHub-native platforms: stdlib health checks that cost nothing, run every 15 minutes under launchd.",
                           "Only failure may invoke a model; the amount of freedom is a level dial from 0 (observe and notify) to 3 (evolve while healthy).",
                           "It exists because a platform once sat frozen for nineteen days with every dashboard green.",
                           "python3 health.py prints a JSON verdict: healthy, degraded or critical, with the failing check ids and why."],
                 "install": ["git clone https://github.com/kody-w/rapp-sentinel && cd rapp-sentinel", "python3 health.py"]},
    "r1r2r3": {"facts": ["R1: receipts aren't evidence — read the artifact, not the log line about it.",
                         "R2: ran isn't worked — a green job with no output is a stall.",
                         "R3: require known-good, never enumerate known-bad.",
                         "These are written down in the sentinel's TRIFECTA-PATTERN document and enforced by its checks."],
               "install": ["git clone https://github.com/kody-w/rapp-sentinel", "python3 health.py"]},
    "neighborhood": {"facts": ["The sentinel's watchers are a declarable roster of AIs from any vendor; each keeps its own rapp/1 chain and can verify the others'.",
                               "The default cast is five: a local daemon, a scout, Copilot, Claude Code, and the brainstem; more are seated by editing config.json.",
                               "A neighbor can be given a cadence: how often it must speak, and which frame kinds count as work — silence then fails a check instead of passing as calm."],
                     "install": ["git clone https://github.com/kody-w/rapp-sentinel", "python3 neighborhood.py roll-call"]},
    "factories": {"facts": ["A factory is an agent that builds agents from a description or transcript, so the unit of work is a working file, not a plan.",
                            "An egg packs a rapplication (a local-first app plus its twin) so it can be hatched on another machine and proven to run.",
                            "The pattern is: describe → generate a file → run it → keep only what runs."],
                  "install": ["git clone https://github.com/kody-w/RAR", "ls agents/@kody-w | grep factory"]},
    "local-first": {"facts": ["Local-first means the runtime, the agents and the records live on the developer's machine; the network is optional, never the source of truth.",
                              "No API keys are needed to run the brainstem or the sentinel; a model can be attached through tools already signed in on the machine.",
                              "It makes the whole system inspectable: every stage is a file you can open."],
                    "install": ["git clone https://github.com/kody-w/rapp-sentinel", "python3 health.py"]},
    "above-not-beside": {"facts": ["'Above AI, not beside it' means the human declares the invariants — what must stay true — and agents do the work underneath them.",
                                   "In practice: checks, receipts, chains and proofs are written by the human once; the models are only allowed to act inside them.",
                                   "The sentinel is the reference implementation: free checks above, model repair below, and the level dial in between."],
                         "install": ["git clone https://github.com/kody-w/rapp-sentinel", "python3 health.py"]},
    "evidence": {"facts": ["Evidence over claims: an artifact is proven by pointing at the thing a stranger can open — a file, a chain, a rendered page — never by a log line saying it happened.",
                           "In RAPP repos this shows up as receipts that name a file hash, proofs that reproduce a failure before fixing it, and checks that read the artifact.",
                           "The rule is the sentinel's R1: receipts aren't evidence."],
                 "install": ["git clone https://github.com/kody-w/rapp-sentinel", "python3 health.py"]},
    "molting": {"facts": ["A running sentinel carries live state written by old code (chains, ledgers, config); an update arrives by git pull into the running install.",
                          "So every change ships a growth path: new config keys default to old behaviour, check ids never rename, frame formats never change, chain history is never rewritten.",
                          "Kody calls this molting: the organism grows a new shell without dying."],
                "install": ["git clone https://github.com/kody-w/rapp-sentinel", "python3 health.py"]},
    "proofs": {"facts": ["Every check change in the sentinel ships a prove_*.py: a stdlib script with a break/control pair that reproduces the OLD blindness before showing the new check fires.",
                         "A proof exits non-zero on deviation, so it can gate a merge; its docstring tells the war story of the failure it prevents.",
                         "The repo's mutation ledger lists every required check and which proof covers it."],
               "install": ["git clone https://github.com/kody-w/rapp-sentinel", "python3 prove_neighbor_moving.py"]},
    "open-estate": {"facts": ["Kody's public estate is hundreds of small repos, each doing one thing, composed by conventions: a registry, chains, checks, static sites.",
                              "Kody2day itself reads that estate every day from the public GitHub API and publishes one page per day.",
                              "The digest separates Kody's own commits from the fleet of bots and automated loops that also push."],
                    "install": ["git clone https://github.com/kody-w/kody2day", "python3 kody2day.py build"]},
}

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
    menu = [{"id": k, "concept": v, "card": CONCEPT_CARDS.get(k, {})} for k, v in CURRICULUM if k not in avoid] \
        or [{"id": k, "concept": v, "card": CONCEPT_CARDS.get(k, {})} for k, v in CURRICULUM]
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
         "subjects, counts) — never invent commits, features, numbers or quotes. Every claim about RAPP itself must come "
         "from the chosen menu item's 'card' (facts + install commands); never describe what a README, doc or test file "
         "contains unless a commit subject in the evidence literally says so; describe commits by their subject lines. "
         "The install section uses ONLY commands from the card. Do not tie the concept to a commit unless the commit "
         "subject plainly illustrates it — say what shipped, then teach the concept, and connect them only where honest. Plain, warm, precise; no hype, no emojis, "
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


def imessage(to, text, files=()):
    """Text the finished episode via Messages.app (macOS). Attachments one by one; oversize files are linked by path.
    Never fatal, never retried into a loop; returns a one-line receipt for the log."""
    if not to:
        return "no KODY2DAY_IMESSAGE set — not texting"
    if sys.platform != "darwin" or not shutil.which("osascript"):
        return "no Messages.app here"
    def _run(script):
        r = subprocess.run(["osascript", "-"], input=script, capture_output=True, text=True, timeout=120)
        return r.returncode == 0, (r.stderr or "").strip()[-200:]
    q = lambda x: str(x).replace("\\", "\\\\").replace('"', '\\"')
    base = ('tell application "Messages"\n set svc to 1st account whose service type = iMessage\n'
            ' set who to participant "%s" of svc\n' % q(to))
    ok, err = _run(base + ' send "%s" to who\nend tell' % q(text))
    sent = ["text" if ok else "text FAILED %s" % err]
    for f in files:
        f = Path(f)
        if not f.exists():
            continue
        mb = f.stat().st_size / 1e6
        if mb > IMESSAGE_MAX_MB:
            _run(base + ' send "%s" to who\nend tell' % q("(%s is %.0f MB — too big for iMessage; it is at %s)" % (f.name, mb, f)))
            sent.append("%s linked (%.0f MB)" % (f.name, mb))
            continue
        ok, err = _run(base + ' send POSIX file "%s" to who\nend tell' % q(str(f)))
        sent.append("%s %s" % (f.name, "sent" if ok else "FAILED " + err))
    return "; ".join(sent)


def stage_notify(ep, episode, draft):
    if not IMESSAGE_TO:
        return "no KODY2DAY_IMESSAGE set — not texting"
    yt = draft.get("youtube") or {}
    files = [v["path"] for k, v in episode["outputs"].items() if v.get("ok")]
    queued = sorted(Path(episode["queue"]).glob("*.mp4"))
    files = [str(f) for f in queued] or files
    lines = ["Kody2day %s — %s" % (episode["date"], "episode ready" if episode["ok"] else "episode INCOMPLETE"),
             yt.get("title") or (draft.get("long") or {}).get("title") or "",
             "concept: %s · refute: %s" % (episode.get("concept"), episode.get("refute"))]
    for k, v in episode["outputs"].items():
        lines.append("%s: %s (%ss)" % (k.replace("_", " "), "ok" if v.get("ok") else "FAILED", v.get("seconds")))
    lines.append("queue: %s" % episode["queue"])
    return imessage(IMESSAGE_TO, "\n".join(l for l in lines if l), files)


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
    rounds = 0
    while refute.get("verdict") == "fail" and rounds < 2:
        rounds += 1
        high = [i for i in refute.get("issues") or [] if i.get("severity") == "high"] or refute.get("issues")
        draft = stage_write(ep, pack, brief, shorts_n, issues=high, previous=draft)
        refute = stage_refute(ep, brief, draft)
        log(ep, "refute (round %d): %s (%d issues)" % (rounds + 1, refute.get("verdict"), len(refute.get("issues") or [])))
    if refute.get("verdict") == "fail":
        (ep / "episode.json").write_text(json.dumps({"date": date, "ok": False, "why": "refuted %d times" % (rounds + 1),
                                                      "issues": refute.get("issues")}, indent=1))
        emit_frame("studio.run", {"date": date, "stage": "refuted", "issues": len(refute.get("issues") or [])})
        log(ep, "refuted %d times — no episode today (drafts kept for a human)" % (rounds + 1))
        return 1
    (ep / "draft.json").write_text(json.dumps(draft, indent=1))
    if skip_render:
        log(ep, "skip_render — scripts written, stopping")
        return 0
    outputs = stage_render(ep, pack, date, draft, tts, quality)
    verified = stage_verify(ep, outputs)
    episode = stage_record(ep, date, draft, refute, verified)
    log(ep, "episode %s: %s — %s" % (date, "OK" if episode["ok"] else "INCOMPLETE",
                                     ", ".join("%s %ss" % (k, v.get("seconds")) for k, v in verified.items())))
    log(ep, "imessage: %s" % stage_notify(ep, episode, draft))
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
