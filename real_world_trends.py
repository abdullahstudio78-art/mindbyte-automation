"""
MindByte Automation - real-world psychology topic sourcing (added 2026-09-03).

Build-order item #1 from claude/real-world-psychology-system-proposal.md:
anchor scripts to a concrete, current, real-world scenario (paired with a
named psychology mechanism) instead of an abstract fact-listicle, since the
channel's own trend research (weekly-trend-reports.md, competitor-research.md)
already found this pattern outperforms abstract facts.

How this fits the existing closed loop, with ZERO changes to pipeline.py,
facebook_pipeline.py, or select_topic_for_run(): this script writes rows
directly into the SAME "NextWeekQueue" tab select_topic_for_run() already
reads every run (see pipeline.py's get_next_queue_brief/NEXT_QUEUE_HEADER).
A real-world-sourced idea is indistinguishable, at read time, from a
weekly_review.py-sourced idea - it's just another queued brief. This is the
same additive-only, self-healing design pattern already used everywhere else
in this codebase.

Two free, zero-payment, ToS-compatible signal sources (per the proposal's
explicit rejection of paid trend APIs):
  1. Reddit's public, unauthenticated JSON listing endpoints (reddit.com/r/
     <sub>/top.json) - no API key required, read-only, pseudonymous by
     platform design (no real names attached to posts already).
  2. This repo's own CompetitorTrends tab (already populated weekly by
     external_trends.py from the public YouTube Data API) - zero new cost,
     zero new credentials, "what's resonating in the niche right now".

Hard compliance gate (per the proposal's Q36 "should not be automated" list
- a hard code gate, never a suggestion an LLM prompt could talk itself
past): every generated idea is regex-scanned for real-name/handle/URL
leakage before it's ever written to the sheet. Anything that fails is
dropped silently (never written), not "fixed" - genericizing a flagged
idea after the fact would just be guessing at a safe rewrite; simplest and
safest is to skip that one candidate and use the next.

Trending-vs-evergreen ratio cap (per the proposal's Q14): hard-capped at a
small number of ideas per run, well under the "no more than ~30% of weekly
output" ceiling recommended until enough trend-driven videos exist to
measure their real performance for this channel.

Every failure mode is best-effort / non-fatal, same spirit as the rest of
the codebase - a failed or empty run should exit cleanly and write zero
rows, never break the workflow it's part of.
"""

import json
import random
import re
import time
from datetime import datetime, timezone

import requests

import pipeline as p

# --- Free-tier, zero-payment real-world signal sources -----------------

# Subreddits chosen for two properties: (a) reliably psychology-relevant
# real-world scenarios (relationship/social/behavioral situations, not news
# events about named individuals), and (b) inherently pseudonymous - Reddit
# posts are never attached to a poster's real name, so the *source* data
# itself carries far less identifiability risk than, say, a viral tweet
# thread or a TikTok drama compilation would.
SOURCE_SUBREDDITS = [
    "relationships", "socialskills", "CasualConversation",
    "DecidingToBeBetter", "offmychest",
]

# Deliberately excludes subreddits whose content skews toward acute crisis,
# self-harm, abuse disclosure, or legal/criminal situations - not because
# psychology can't be discussed around those topics, but because this
# automated, unattended sourcing step has no human review before the idea
# reaches the queue, and Q36 of the proposal is explicit that anything
# touching a real person's specific crisis needs human judgment, not a
# script. r/AmItheAsshole was deliberately left out of this list for the
# same reason - too much of its content centers on identifiable specific
# disputes between named (if pseudonymous) real people in a way a generic
# "someone" retelling doesn't fully launder.
# Reddit's own API rules ask for "platform:app_id:version (by /u/username)"
# style user agents and are known to 403 generic/bot-looking UAs, especially
# from cloud/CI IP ranges (GitHub Actions runners included) - verified live
# on run #2 (2026-09-03): all 5 subreddits 403'd with the previous generic
# UA. This is a best-effort source either way (see fetch_reddit_candidates'
# non-fatal per-subreddit try/except) - CompetitorTrends alone already
# supplies real candidates every run, so a still-failing Reddit fetch here
# degrades gracefully rather than blocking anything.
REDDIT_HEADERS = {"User-Agent": "python:mindbyte-real-world-trends:v1.0 (by /u/mindbyte_automation)"}

MAX_POSTS_PER_SUB = 8
MAX_IDEAS_PER_RUN = 5  # small cap: proposal's ~30%-of-weekly-output ceiling, kept well under it
MAX_LONGFORM_IDEAS_PER_RUN = 1

COMPETITOR_TRENDS_RANGE = "CompetitorTrends!A2:H"

# --- Hard compliance / genericization gate ------------------------------

# Reject-outright markers: anything that looks like it names, tags, or
# links a specific real person/account. This runs on the GENERATED idea
# text (title/hook/angle/seo fields), not just the source post, since the
# risk is the output leaking an identifiable reference, not the input.
_BANNED_PATTERNS = [
    re.compile(r"/?u/\w+", re.IGNORECASE),       # reddit username reference
    re.compile(r"/?r/\w+", re.IGNORECASE),       # subreddit reference (would out the source oddly)
    re.compile(r"@\w+"),                          # social handle
    re.compile(r"https?://\S+"),                  # any URL
    re.compile(r"\bTikTok\b|\bInstagram\b|\bTwitter\b|\bX\.com\b", re.IGNORECASE),
]

# Real-name detection: deliberately NOT applied to title-style fields
# (title/seo_title/thumbnail_concept/seo_tags), because those are Title
# Case by construction - "The Halo Effect", "Free Will", "Social Proof",
# "Tiny Grievances" all capitalize every content word, which made the first
# version of this check (any two consecutive capitalized words) flag every
# single idea a run produced (verified live: 15/15 rejected on run #2,
# 2026-09-03 - a totally silent 100% false-positive rate is worse than no
# gate at all, since it looks like the gate is working while it's actually
# just discarding everything). A real name is a signal worth catching in
# normal SENTENCE-case prose (hook/angle/seo_description), where capitals
# are meaningful, not in headline-style fields where every word is
# capitalized regardless of content.
PROSE_FIELDS = ("hook", "angle", "seo_description")
TITLE_STYLE_FIELDS = ("title", "seo_title", "thumbnail_concept")

_SAFE_CAPITALIZED_PHRASES = {
    "psychology of", "the psychology", "social psychology", "human behavior",
    "emotional intelligence", "brain and", "attachment styles",
    "social proof", "free will", "halo effect", "peak end", "self efficacy",
}


def _looks_like_real_name(text: str) -> bool:
    """Only meaningful on sentence-case prose. A capitalized two-word match
    is skipped when it opens the string or follows sentence-ending
    punctuation, since that capitalization is just normal English grammar,
    not a name."""
    for match in re.finditer(r"\b([A-Z][a-z]+)\s+([A-Z][a-z]+)\b", text):
        phrase = match.group(0).lower()
        if phrase in _SAFE_CAPITALIZED_PHRASES:
            continue
        start = match.start()
        preceding = text[:start].rstrip()
        if start == 0 or preceding.endswith((".", "!", "?", ":")):
            continue  # sentence-initial capitalization, not a name signal
        return True
    return False


def compliance_gate(idea: dict) -> tuple:
    """Returns (passed: bool, reason: str). This is the hard gate from the
    proposal - a failure here means the idea is dropped, never rewritten/
    auto-fixed. Banned-pattern (URL/handle/platform) scanning covers every
    generated field; the real-name heuristic only runs on prose fields
    (see PROSE_FIELDS above) since it produces near-100% false positives on
    Title Case headline fields."""
    all_fields = [idea.get(f, "") for f in (*PROSE_FIELDS, *TITLE_STYLE_FIELDS)]
    all_fields.append(" ".join(idea.get("seo_tags", []) or []))
    joined_all = " | ".join(all_fields)
    for pattern in _BANNED_PATTERNS:
        if pattern.search(joined_all):
            return False, f"banned pattern matched: {pattern.pattern}"

    prose_text = " ".join(idea.get(f, "") for f in PROSE_FIELDS)
    if _looks_like_real_name(prose_text):
        return False, "generated prose contains what looks like a real full name"

    if not idea.get("genericized_ok", True):
        return False, "model itself flagged this scenario as not safely genericizable"
    return True, "OK"


# --- Reddit sourcing ------------------------------------------------------

def fetch_reddit_candidates() -> list:
    """Best-effort: pulls this week's top posts from a small, deliberately
    non-crisis subreddit list. Returns a list of {"subreddit","title",
    "selftext"} dicts. Any failure (rate limit, network, empty) returns an
    empty list rather than raising."""
    candidates = []
    session = requests.Session()
    for sub in SOURCE_SUBREDDITS:
        try:
            resp = session.get(
                f"https://www.reddit.com/r/{sub}/top.json",
                headers=REDDIT_HEADERS,
                params={"limit": MAX_POSTS_PER_SUB, "t": "week"},
                timeout=20,
            )
            if resp.status_code != 200:
                print(f"[real_world_trends] reddit r/{sub} returned {resp.status_code} - skipping")
                continue
            children = resp.json().get("data", {}).get("children", [])
            for child in children:
                d = child.get("data", {})
                title = (d.get("title") or "").strip()
                if not title or d.get("over_18"):
                    continue
                candidates.append({
                    "subreddit": sub,
                    "title": title,
                    "selftext": (d.get("selftext") or "")[:600],
                })
        except Exception as e:  # noqa: BLE001 - one bad subreddit must not stop the run
            print(f"[real_world_trends] reddit r/{sub} failed (non-fatal): {e}")
        time.sleep(1)  # polite pacing between unauthenticated public requests
    return candidates


def fetch_competitor_trend_candidates(access_token: str) -> list:
    """Reuses this repo's own CompetitorTrends tab (already populated
    weekly by external_trends.py) as a second, zero-new-cost real-world-
    adjacent signal: what's actually resonating in the psychology niche
    right now. Returns titles only - these are just inspiration, never
    copied (see external_trends.py's own docstring for that policy)."""
    try:
        rows = p.sheet_get(access_token, COMPETITOR_TRENDS_RANGE)
    except Exception as e:  # noqa: BLE001 - tab may not exist yet
        print(f"[real_world_trends] CompetitorTrends not available ({e}) - skipping that source")
        return []
    # CompetitorTrends accumulates every week since 2026-07-31 (external_trends.py
    # appends, never trims) - by now that's hundreds of rows. Only the most
    # recent batch is "currently resonating"; older rows are stale by
    # definition for a real-world-trend signal. Take only the tail (most
    # recently appended rows) instead of the whole sheet history - this is
    # also what keeps this function from single-handedly blowing the
    # per-run Groq-call budget (see MAX_CANDIDATES_TO_EVALUATE below).
    rows = rows[-40:]
    candidates = []
    for row in rows:
        row = row + [""] * (8 - len(row))
        title = (row[2] or "").strip()
        if title:
            candidates.append({"subreddit": "", "title": title, "selftext": ""})
    return candidates


# --- Groq: genericize + build a full brief --------------------------------

PILLAR_KEYS = list(p.CONTENT_PILLARS.keys())

BRIEF_PROMPT = """You write short-form psychology content briefs for a YouTube/Facebook channel called MindByte.

A real-world scenario was noticed as currently resonating (source below). Your job:

1. Identify the SPECIFIC, real, citable psychology mechanism this scenario illustrates (not a vague "emotions are complex" - a named concept: e.g. cognitive dissonance, attachment anxiety, sunk cost fallacy, mirroring, etc.)
2. GENERICIZE the scenario completely: never use any real name, username, handle, platform mention, or identifying detail. Write it as "a viral story where someone..." / "a situation where a person...". If you cannot genericize this scenario without it still being identifiable as a specific real person or event, set "genericized_ok" to false and leave other fields blank - do not attempt a partial genericization.
3. Build a full content brief in MindByte's existing voice: hook -> relatable problem -> curiosity gap -> mechanism -> example -> insight. Never mention the source platform or that this came from a "post" or "thread" - just the psychology story.

Source scenario (for your reference only, never quote or reference where it came from): "{source_title}" {source_extra}

Return ONLY valid JSON:
{{
  "genericized_ok": true or false,
  "pillar": "<one of: {pillars}>",
  "title": "<internal working title>",
  "hook": "<opening line>",
  "angle": "<the unique angle/mechanism>",
  "seo_title": "<YouTube-optimized title, no clickbait lies>",
  "seo_description": "<2-3 sentence description>",
  "seo_tags": ["tag1", "tag2", "..."],
  "target_length_sec": 55,
  "hook_type": "<short label, e.g. 'question', 'confession', 'twist'>",
  "thumbnail_concept": "<one-line visual concept, no text-on-thumbnail needed>"
}}"""


def build_brief_from_candidate(candidate: dict) -> dict:
    source_extra = f"(additional context: {candidate['selftext']})" if candidate.get("selftext") else ""
    prompt = BRIEF_PROMPT.format(
        source_title=candidate["title"], source_extra=source_extra,
        pillars=", ".join(PILLAR_KEYS),
    )
    try:
        raw = p.call_groq(prompt)
        idea = json.loads(raw)
    except Exception as e:  # noqa: BLE001 - one bad candidate must not stop the run
        print(f"[real_world_trends] brief generation failed for '{candidate['title'][:60]}': {e}")
        return None
    if idea.get("pillar") not in p.CONTENT_PILLARS:
        idea["pillar"] = PILLAR_KEYS[0]
    return idea


# --- Write to NextWeekQueue -----------------------------------------------

def write_idea_to_queue(access_token: str, idea: dict, fmt: str, week_of: str) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    row = [
        week_of, fmt, idea.get("pillar", ""), idea.get("title", ""),
        idea.get("hook", ""), idea.get("angle", ""),
        idea.get("seo_title", ""), idea.get("seo_description", ""),
        ", ".join(idea.get("seo_tags", []) or []),
        "",  # CTAStyle - left blank, downstream cta_style rotation picks its own as usual
        idea.get("target_length_sec", ""),
        "N", now_iso,
        idea.get("hook_type", "unclassified"), "",
        idea.get("thumbnail_concept", ""), "", "",
        "Medium",  # Confidence: this is a fresh trend idea, not yet performance-validated
    ]
    try:
        p.sheet_append(access_token, "NextWeekQueue!A:S", row)
    except Exception:
        if p.ensure_sheet_tab(access_token, "NextWeekQueue", p.NEXT_QUEUE_HEADER):
            p.sheet_append(access_token, "NextWeekQueue!A:S", row)
        else:
            print("[real_world_trends] could not write idea to NextWeekQueue (tab missing and self-heal failed)")


def run() -> None:
    access_token = p.get_access_token()
    week_of = datetime.now(timezone.utc).date().isoformat()

    raw_candidates = fetch_reddit_candidates() + fetch_competitor_trend_candidates(access_token)
    print(f"[real_world_trends] {len(raw_candidates)} raw candidates gathered")
    if not raw_candidates:
        print("[real_world_trends] no candidates available this run - exiting cleanly")
        return

    # Run #1 (2026-09-03) timed out at 8 minutes: nothing capped how many
    # candidates got a full Groq call before the run gave up trying to hit
    # MAX_IDEAS_PER_RUN, so a run with a lot of compliance-gate rejections
    # (or a large CompetitorTrends backlog) just kept calling Groq
    # sequentially until the workflow's own timeout killed it mid-run,
    # silently writing zero rows despite real API spend. Shuffle first (so
    # the hard evaluation cap doesn't always favor whichever source
    # happened to come first) and hard-cap total Groq calls per run
    # regardless of how many candidates end up rejected - a run should
    # always finish and report a real result, even if that result is
    # "found fewer good ideas than the target this week."
    random.shuffle(raw_candidates)
    MAX_CANDIDATES_TO_EVALUATE = 15
    evaluated = 0

    short_written, longform_written = 0, 0
    for candidate in raw_candidates:
        if short_written >= MAX_IDEAS_PER_RUN and longform_written >= MAX_LONGFORM_IDEAS_PER_RUN:
            break
        if evaluated >= MAX_CANDIDATES_TO_EVALUATE:
            print(f"[real_world_trends] hit the {MAX_CANDIDATES_TO_EVALUATE}-candidate evaluation cap "
                  f"for this run - stopping here rather than risking a timeout")
            break
        evaluated += 1
        idea = build_brief_from_candidate(candidate)
        if idea is None:
            continue
        passed, reason = compliance_gate(idea)
        if not passed:
            print(f"[real_world_trends] dropped idea '{idea.get('title', '')[:60]}' - {reason}")
            continue
        if short_written < MAX_IDEAS_PER_RUN:
            write_idea_to_queue(access_token, idea, "short", week_of)
            short_written += 1
        elif longform_written < MAX_LONGFORM_IDEAS_PER_RUN:
            write_idea_to_queue(access_token, idea, "longform", week_of)
            longform_written += 1

    print(f"[real_world_trends] done - wrote {short_written} short + {longform_written} longform "
          f"real-world-sourced briefs to NextWeekQueue")


def main() -> None:
    try:
        run()
    except Exception as e:  # noqa: BLE001 - must never fail the workflow it's part of
        print(f"[real_world_trends] run failed unexpectedly (non-fatal, exiting cleanly): {e}")


if __name__ == "__main__":
    main()
