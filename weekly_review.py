"""
MindByte Automation - weekly self-improving analysis.

Runs once a week (Sundays, via .github/workflows/weekly_review.yml) after
analytics_sync.py has kept view/retention/subscriber counts current all
week. This is the "learning" half of the closed loop described in
PIPELINE_PLAN / the 2026-07-30 self-improvement build:

    Upload -> collect analytics -> store history -> analyze performance ->
    learn winning/losing patterns -> generate weekly insights ->
    generate better content -> feed it into the existing pipeline -> repeat.

What this script does, in order:
  1. Reads every logged video from the "Videos" tab (existing), the latest
     snapshot per video from "AnalyticsHistory" (new, written daily by
     analytics_sync.py), and each video's structural metadata from
     "VideoMeta" (new, written at publish time by pipeline.py /
     pipeline_longform.py).
  2. Computes a composite performance score per video from views,
     retention (average view percentage), average view duration, and
     engagement (likes+comments/views) - plus subscribers-gained and
     subscriber-conversion-rate once any subscriber data exists at all
     (the channel starts at 0 subscribers; this script degrades
     gracefully rather than dividing by a metric that's always zero).
     Scores are RANK-based (percentile within the current video set), not
     raw-value based, and every group aggregate below is a recency-
     weighted average (recent videos count more, but nothing is ever
     decided from a single video) - both choices exist specifically so one
     viral outlier can't dominate the whole learning signal.
  3. Groups videos by pillar, hook opener, script structure, word-count
     band, video-length band, upload hour, and individual SEO tag, and
     reports the best/worst group in each dimension (requiring at least 2
     videos per group before calling it a pattern, otherwise flagged as
     "not enough data yet").
  4. Sends the whole comparison to Groq for a structured JSON report:
     what worked, what failed, new trends, and a concrete list of
     ready-to-use next-video briefs (title/hook/angle/SEO/CTA/target
     length, tagged short or long-form).
  5. Writes a human-readable summary to "WeeklyPlan" (extends the existing
     tab/columns, doesn't remove anything) AND writes each next-video brief
     as its own row in "NextWeekQueue" - this second part is what closes
     the loop: pipeline.py's and pipeline_longform.py's select_topic_for_run()
     read directly from that tab before falling back to a random pick, so
     no manual copying is required for this week's learnings to shape next
     week's videos.

Everything here degrades gracefully: a missing tab self-heals (same
pattern as the rest of this codebase), a Groq or Sheets failure logs and
returns rather than raising, and if there isn't enough published-video
data yet, the script says so and exits cleanly instead of guessing.
"""

import json
import math
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
OAUTH_CLIENT_ID = os.environ["OAUTH_CLIENT_ID"]
OAUTH_CLIENT_SECRET = os.environ["OAUTH_CLIENT_SECRET"]
OAUTH_REFRESH_TOKEN = os.environ["OAUTH_REFRESH_TOKEN"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]

GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
SHEETS_BASE = f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_SHEET_ID}"
SESSION = requests.Session()

WEEKLY_PLAN_HEADER = [
    "WeekOf", "TopPerformers", "WeakPerformers", "PlanNotes",
    "PatternSummary", "SubscriberInsights", "GeneratedAt",
]
NEXT_QUEUE_HEADER = [
    "WeekOf", "Format", "Pillar", "Title", "Hook", "Angle",
    "SEOTitle", "SEODescription", "SEOTags", "CTAStyle",
    "TargetLengthSec", "Used", "CreatedAt",
    # Appended at end 2026-07-31 for the confidence/loyalty upgrade:
    "HookType", "Series", "ThumbnailConcept", "ChapterOutline",
    "LoyaltyAngle", "Confidence",
]

WEEKLY_REPORT_FULL_TAB = "WeeklyReportFull"
WEEKLY_REPORT_FULL_HEADER = ["Date", "Section", "Content"]

COMPETITOR_TRENDS_HEADER = [
    "Date", "Query", "Title", "ChannelTitle", "Views", "DurationSec", "PublishedAt", "Notes",
]

MIN_GROUP_SAMPLE = 2          # don't call something a "pattern" off one video
RECENCY_HALF_LIFE_DAYS = 30   # recent performance counts more, nothing decided off one video
# Raised from 5 to 14 on 2026-08-01 when publish.yml moved from 1/day to
# 2/day Shorts (14/week). Keep this in sync with the Shorts publish cadence -
# if it falls behind weekly consumption again, pipeline.py's fallback to the
# original random/idea-scored topic picker kicks in silently for the
# uncovered days, which is safe but means those days skip the weekly-review
# briefs entirely.
NEW_SHORTS_IDEAS_PER_WEEK = 14
NEW_LONGFORM_IDEAS_PER_WEEK = 1

# --- Confidence rule (documented here, used by confidence_for_group()) ---
# High   = effective sample size (recency-weighted, see below) >= 8 AND the
#          top group's score meaningfully exceeds the bottom group's score
#          (>= CONFIDENCE_SEPARATION_MIN, i.e. not a noise-level gap).
# Medium = MIN_GROUP_SAMPLE (2) <= effective sample size < 8, OR the group
#          is directionally consistent but the separation is borderline.
# Low    = effective sample size < MIN_GROUP_SAMPLE, or the separation is
#          not meaningfully different from noise. In Low-confidence cases
#          the system must say "insufficient data, continue collecting"
#          rather than asserting a strategy change.
# "Effective sample size" here is a simple count-based proxy (sum of each
# group's recency_weight values, i.e. a recency-discounted count) rather
# than full effective-sample-size statistics - documented explicitly since
# the exact statistical ESS formula was judged impractical to compute
# reliably from this data volume.
CONFIDENCE_HIGH_ESS = 8
CONFIDENCE_SEPARATION_MIN = 0.08  # composite/percentile-rank-scale score gap


# ---------------------------------------------------------------------------
# Google auth / Sheets helpers (same pattern as the rest of the codebase)
# ---------------------------------------------------------------------------

def get_access_token() -> str:
    resp = SESSION.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": OAUTH_CLIENT_ID,
            "client_secret": OAUTH_CLIENT_SECRET,
            "refresh_token": OAUTH_REFRESH_TOKEN,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _sheets_call_with_retry(fn, _retries: int = 2):
    """Same 429/5xx retry-with-backoff idea already used for Groq calls
    (call_groq(..., _retries=2) below), applied to Sheets HTTP calls."""
    last_exc = None
    for attempt in range(_retries + 1):
        try:
            resp = fn()
        except requests.RequestException as e:
            last_exc = e
            if attempt < _retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < _retries:
            wait_s = 1.5 * (attempt + 1)
            print(f"[weekly] Sheets call got {resp.status_code} - retrying in {wait_s:.1f}s")
            time.sleep(wait_s)
            continue
        return resp
    if last_exc:
        raise last_exc


def sheet_get(token: str, a1_range: str) -> list:
    resp = _sheets_call_with_retry(
        lambda: SESSION.get(f"{SHEETS_BASE}/values/{a1_range}", headers=headers(token), timeout=30)
    )
    if resp.status_code == 400:
        return []  # tab doesn't exist yet - treat as empty, not fatal
    resp.raise_for_status()
    return resp.json().get("values", [])


def sheet_append(token: str, a1_range: str, row: list) -> None:
    resp = _sheets_call_with_retry(
        lambda: SESSION.post(
            f"{SHEETS_BASE}/values/{a1_range}:append",
            headers=headers(token),
            params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
            json={"values": [row]},
            timeout=30,
        )
    )
    resp.raise_for_status()


def ensure_sheet_tab(token: str, tab_name: str, header_row: list) -> bool:
    try:
        resp = SESSION.post(
            f"{SHEETS_BASE}:batchUpdate",
            headers=headers(token),
            json={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
            timeout=30,
        )
        if resp.status_code != 200:
            return False
        sheet_append(token, f"{tab_name}!A:Z", header_row)
        return True
    except Exception:
        return False


def append_with_selfheal(token: str, tab_name: str, a1_range: str, header: list, row: list) -> None:
    try:
        sheet_append(token, a1_range, row)
    except Exception as e:  # noqa: BLE001 - logging must never crash the run
        print(f"[weekly] {tab_name} append failed ({e}), attempting to create it")
        if ensure_sheet_tab(token, tab_name, header):
            try:
                sheet_append(token, a1_range, row)
            except Exception as e2:
                print(f"[weekly] still could not append to {tab_name} after creating it: {e2}")
        else:
            print(f"[weekly] could not create {tab_name} tab - row not logged")


def call_groq(prompt: str, _retries: int = 2) -> str:
    for attempt in range(_retries + 1):
        resp = SESSION.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": GROQ_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.4,
                "response_format": {"type": "json_object"},
            },
            timeout=60,
        )
        if resp.status_code == 429 and attempt < _retries:
            wait_s = 5.0
            match = re.search(r"try again in ([\d.]+)s", resp.text)
            if match:
                wait_s = float(match.group(1)) + 1.0
            print(f"[weekly] Groq rate-limited - waiting {wait_s:.1f}s and retrying")
            import time
            time.sleep(wait_s)
            continue
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()


def safe_int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def safe_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Data loading / merging
# ---------------------------------------------------------------------------

def load_videos(token: str) -> list:
    rows = sheet_get(token, "Videos!A2:O")
    out = []
    for row in rows:
        row = row + [""] * (15 - len(row))
        video_id, title, topic, status = row[0], row[1], row[2], row[3]
        if not video_id or status not in ("Scheduled", "Published"):
            continue
        out.append({
            "video_id": video_id, "title": title, "topic": topic,
            "created_date": row[4], "publish_at": row[5],
            "views": safe_int(row[8]), "likes": safe_int(row[9]),
            "comments": safe_int(row[10]), "shares": safe_int(row[11]),
        })
    return out


def load_longform_videos(token: str) -> list:
    """Long-form counterpart of load_videos() (2026-07-31), reading the
    LongformVideos tab written by pipeline_longform.py. Views/likes/
    comments here come from the new O:S columns analytics_sync.py appends
    (see analytics_sync.py's sync_longform_videos()) - old rows without
    those columns simply read as 0, same graceful degrade as everywhere
    else in this file. Best-effort: an empty/missing tab just yields []."""
    rows = sheet_get(token, "LongformVideos!A2:S")
    out = []
    for row in rows:
        row = row + [""] * (19 - len(row))
        video_id, title, topic, status = row[0], row[1], row[2], row[3]
        if not video_id or status not in ("Scheduled", "Published"):
            continue
        out.append({
            "video_id": video_id, "title": title, "topic": topic,
            "created_date": row[4], "publish_at": row[5],
            "views": safe_int(row[14]), "likes": safe_int(row[15]),
            "comments": safe_int(row[16]), "shares": safe_int(row[17]),
        })
    return out


def load_analytics_history_latest(token: str) -> dict:
    """One row per video_id: the MOST RECENT snapshot in AnalyticsHistory,
    keyed by video_id. History rows accumulate daily, so this is a
    date-sorted dict-overwrite rather than a separate query."""
    rows = sheet_get(token, "AnalyticsHistory!A2:O")
    latest = {}
    for row in rows:
        row = row + [""] * (15 - len(row))
        date, video_id = row[0], row[1]
        if not video_id:
            continue
        entry = {
            "date": date, "avg_view_duration": safe_float(row[10]),
            "avg_view_pct": safe_float(row[11]), "subs_gained": safe_int(row[12]),
        }
        prev = latest.get(video_id)
        if prev is None or date >= prev["date"]:
            latest[video_id] = entry
    return latest


def load_video_meta(token: str) -> dict:
    """Reads VideoMeta!A2:Q (extended from A2:N 2026-07-31 with HookType,
    Series, ThumbnailIdentity appended at the end). Rows written before the
    upgrade will simply be shorter than 17 cells - the padding below fills
    those with "" so hook_type/series gracefully read as unset ("") rather
    than raising, exactly as the spec requires."""
    rows = sheet_get(token, "VideoMeta!A2:Q")
    meta = {}
    for row in rows:
        row = row + [""] * (17 - len(row))
        video_id = row[0].strip()
        if not video_id:
            continue
        meta[video_id] = {
            "pillar": row[3], "format": row[4], "hook_text": row[5],
            "hook_opener": row[6], "structure": row[7],
            "word_count": safe_int(row[8]), "unit_count": safe_int(row[9]),
            "length_sec": safe_float(row[10]), "upload_hour": row[11],
            "tags": [t.strip() for t in row[12].split(",") if t.strip()],
            "hook_type": (row[14] or "").strip(),
            "series": (row[15] or "").strip(),
        }
    return meta


def load_competitor_trends(token: str) -> list:
    """Best-effort, empty-safe read of CompetitorTrends (written weekly by
    external_trends.py). Returns [] on any failure or if the tab/data
    doesn't exist yet - this is optional inspiration input only."""
    try:
        rows = sheet_get(token, "CompetitorTrends!A2:H")
    except Exception:
        return []
    out = []
    for row in rows:
        row = row + [""] * (8 - len(row))
        if not row[2]:
            continue
        out.append({
            "query": row[1], "title": row[2], "channel_title": row[3],
            "views": safe_int(row[4]), "duration_sec": safe_int(row[5]), "notes": row[7],
        })
    return out


def merge_records(videos: list, history: dict, meta: dict) -> list:
    merged = []
    for v in videos:
        h = history.get(v["video_id"], {})
        m = meta.get(v["video_id"], {})
        views = v["views"]
        engagement_rate = (v["likes"] + v["comments"]) / views if views > 0 else 0.0
        rec = {
            **v,
            "avg_view_duration": h.get("avg_view_duration", 0.0),
            "avg_view_pct": h.get("avg_view_pct", 0.0),
            "subs_gained": h.get("subs_gained", 0),
            "engagement_rate": engagement_rate,
            "pillar": m.get("pillar", "") or "(unknown)",
            "format": m.get("format", "") or "short",
            "hook_opener": (m.get("hook_opener", "") or "").lower().strip(),
            "structure": m.get("structure", "") or "(unknown)",
            "word_count": m.get("word_count", 0),
            "length_sec": m.get("length_sec", 0.0),
            "upload_hour": m.get("upload_hour", ""),
            "tags": m.get("tags", []),
            # New pattern dimensions (2026-07-31) - blank/absent for older
            "hook_type": m.get("hook_type", "") or "",
            "series": m.get("series", "") or "",
        }
        merged.append(rec)
    return merged


# ---------------------------------------------------------------------------
# Scoring: rank-based (percentile) composite score + recency weighting
# ---------------------------------------------------------------------------

def percentile_ranks(values: list) -> list:
    """Rank-based normalization to 0-1 - robust to a single outlier value
    the way min-max or z-score scaling isn't (one viral video can't blow
    out the whole scale, it just ranks #1)."""
    n = len(values)
    if n <= 1:
        return [0.5 for _ in values]
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    for position, idx in enumerate(order):
        ranks[idx] = position / (n - 1)
    return ranks


def recency_weight(publish_at_iso: str) -> float:
    """Exponential decay so recent videos count more toward pattern
    aggregates without ever fully zeroing out older data - avoids both
    "ignore everything but last week" and "treat a 6-month-old video as
    equally informative as yesterday's"."""
    if not publish_at_iso:
        return 0.5
    try:
        published = datetime.fromisoformat(publish_at_iso.replace("Z", "+00:00"))
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        days_ago = max(0.0, (datetime.now(timezone.utc) - published).total_seconds() / 86400)
    except Exception:
        return 0.5
    return 0.5 ** (days_ago / RECENCY_HALF_LIFE_DAYS)


def compute_composite_scores(records: list) -> None:
    """Mutates each record in place, adding "composite_score" (0-1) and
    "recency_weight". Subscriber signal is only folded in once ANY video
    in the set has subs_gained > 0 - until then the channel is brand new
    (0 subscribers) and this optimizes purely for views/retention/
    duration/engagement, exactly as required."""
    if not records:
        return
    has_subscriber_data = any(r["subs_gained"] > 0 for r in records)

    views_pr = percentile_ranks([r["views"] for r in records])
    retention_pr = percentile_ranks([r["avg_view_pct"] for r in records])
    duration_pr = percentile_ranks([r["avg_view_duration"] for r in records])
    engagement_pr = percentile_ranks([r["engagement_rate"] for r in records])
    if has_subscriber_data:
        subs_pr = percentile_ranks([r["subs_gained"] for r in records])
        weights = {"views": 0.20, "retention": 0.25, "duration": 0.15, "engagement": 0.15, "subs": 0.25}
    else:
        subs_pr = [0.0] * len(records)
        weights = {"views": 0.25, "retention": 0.30, "duration": 0.20, "engagement": 0.25, "subs": 0.0}

    for i, r in enumerate(records):
        r["composite_score"] = (
            weights["views"] * views_pr[i]
            + weights["retention"] * retention_pr[i]
            + weights["duration"] * duration_pr[i]
            + weights["engagement"] * engagement_pr[i]
            + weights["subs"] * subs_pr[i]
        )
        r["recency_weight"] = recency_weight(r["publish_at"])
        r["subscriber_conversion_rate"] = (
            r["subs_gained"] / r["views"] if has_subscriber_data and r["views"] > 0 else 0.0
        )


def weighted_group_average(records: list, key_fn, value_fn=lambda r: r["composite_score"]) -> dict:
    """Groups records by key_fn(record), returns {group_key: {"score":
    recency-weighted average value, "n": sample count}}."""
    buckets = defaultdict(list)
    for r in records:
        buckets[key_fn(r)].append(r)
    out = {}
    for key, group in buckets.items():
        total_w = sum(r["recency_weight"] for r in group)
        if total_w <= 0:
            avg = sum(value_fn(r) for r in group) / len(group)
        else:
            avg = sum(value_fn(r) * r["recency_weight"] for r in group) / total_w
        out[key] = {"score": avg, "n": len(group)}
    return out


def top_bottom(groups: dict, min_n: int = MIN_GROUP_SAMPLE) -> tuple:
    """Returns (best_key, worst_key) among groups with enough samples, or
    (None, None) if nothing has enough data yet."""
    eligible = {k: v for k, v in groups.items() if v["n"] >= min_n}
    if not eligible:
        return None, None
    ranked = sorted(eligible.items(), key=lambda kv: kv[1]["score"], reverse=True)
    return ranked[0][0], ranked[-1][0]


def word_count_bucket(wc: int) -> str:
    if wc <= 0:
        return "unknown"
    if wc < 140:
        return "under 140 words"
    if wc <= 190:
        return "140-190 words"
    if wc <= 1400:
        return "191-1400 words"
    return "1400+ words (long-form)"


def length_bucket(sec: float) -> str:
    if sec <= 0:
        return "unknown"
    if sec < 45:
        return "under 45s"
    if sec <= 65:
        return "45-65s"
    if sec <= 90:
        return "66-90s"
    if sec < 480:
        return "91-479s"
    return "8min+ (long-form)"


def hour_bucket(hour_str: str) -> str:
    try:
        h = int(hour_str)
    except (TypeError, ValueError):
        return "unknown"
    block_start = (h // 3) * 3
    return f"{block_start:02d}:00-{(block_start + 3) % 24:02d}:00 UTC"


def confidence_for_group(groups: dict, best_key, worst_key) -> str:
    """Confidence rule - see the CONFIDENCE_* constants' doc comment above
    for the exact formula. Returns "High" / "Medium" / "Low"."""
    if best_key is None:
        return "Low"
    best = groups[best_key]
    ess = best["n"]  # count-based proxy for effective sample size (documented above)
    if ess < MIN_GROUP_SAMPLE:
        return "Low"
    if worst_key is not None and worst_key != best_key:
        separation = best["score"] - groups[worst_key]["score"]
    else:
        separation = 0.0
    if ess >= CONFIDENCE_HIGH_ESS and separation >= CONFIDENCE_SEPARATION_MIN:
        return "High"
    if ess >= MIN_GROUP_SAMPLE:
        return "Medium"
    return "Low"


def detect_patterns(records: list) -> dict:
    patterns = {}

    def add(dimension: str, key_fn, value_fn=lambda r: r["composite_score"]):
        groups = weighted_group_average(records, key_fn, value_fn)
        best, worst = top_bottom(groups)
        patterns[dimension] = {
            "best": {"key": best, **groups[best]} if best else None,
            "worst": {"key": worst, **groups[worst]} if worst and worst != best else None,
            "groups_seen": len(groups),
            "confidence": confidence_for_group(groups, best, worst),
        }

    add("topic", lambda r: r["topic"] or "(untitled)")
    add("pillar", lambda r: r["pillar"])
    add("hook_opener", lambda r: r["hook_opener"] or "(unknown)")
    add("script_structure", lambda r: r["structure"])
    add("word_count_band", lambda r: word_count_bucket(r["word_count"]))
    add("video_length_band", lambda r: length_bucket(r["length_sec"]))
    add("upload_hour_band", lambda r: hour_bucket(r["upload_hour"]))

    # New pattern dimensions (2026-07-31): hook_type and series, read from
    # VideoMeta's new columns. Gracefully skipped (not added to `patterns`
    # at all) if no record has a non-blank value yet, per spec.
    if any(r.get("hook_type") for r in records):
        add("hook_type", lambda r: r["hook_type"] or "(unclassified)")
    if any(r.get("series") for r in records):
        add("series", lambda r: r["series"] or "(none)")

    # Keyword/tag-level pattern: a video can carry many tags, so this is a
    # one-to-many expansion rather than a straight groupby.
    tag_records = []
    for r in records:
        for tag in r["tags"]:
            tag_records.append({**r, "_tag": tag.lower()})
    if tag_records:
        groups = weighted_group_average(tag_records, lambda r: r["_tag"])
        best, worst = top_bottom(groups)
        patterns["seo_keyword"] = {
            "best": {"key": best, **groups[best]} if best else None,
            "worst": {"key": worst, **groups[worst]} if worst and worst != best else None,
            "groups_seen": len(groups),
            "confidence": confidence_for_group(groups, best, worst),
        }

    has_subscriber_data = any(r["subs_gained"] > 0 for r in records)
    if has_subscriber_data:
        add("subscriber_pillar", lambda r: r["pillar"], value_fn=lambda r: r["subscriber_conversion_rate"])
        add("subscriber_hook", lambda r: r["hook_opener"] or "(unknown)", value_fn=lambda r: r["subscriber_conversion_rate"])
        add("subscriber_length_band", lambda r: length_bucket(r["length_sec"]), value_fn=lambda r: r["subscriber_conversion_rate"])

    return patterns, has_subscriber_data


# ---------------------------------------------------------------------------
# Report + next-week content brief generation
# ---------------------------------------------------------------------------

def format_pattern_line(dimension: str, data: dict) -> str:
    confidence = data.get("confidence", "Low")
    if data["best"] is None:
        return (f"- {dimension}: insufficient data, continue collecting "
                f"(need at least {MIN_GROUP_SAMPLE} videos per group) [confidence: {confidence}]")
    line = (f"- {dimension}: best = \"{data['best']['key']}\" (score {data['best']['score']:.3f}, "
            f"n={data['best']['n']})")
    if data["worst"]:
        line += f"; worst = \"{data['worst']['key']}\" (score {data['worst']['score']:.3f}, n={data['worst']['n']})"
    line += f" [confidence: {confidence}]"
    if confidence == "Low":
        line += " - insufficient data, continue collecting rather than asserting a strategy change"
    return line


def build_groq_prompt(records: list, patterns: dict, has_subscriber_data: bool, competitor_trends: list) -> str:
    by_composite = sorted(records, key=lambda r: r["composite_score"], reverse=True)
    top_n = by_composite[:5]
    bottom_n = by_composite[-5:] if len(by_composite) > 5 else []

    def summarize(v):
        return (
            f"\"{v['title']}\" (topic: {v['topic']}, pillar: {v['pillar']}, format: {v['format']}) - "
            f"{v['views']} views, retention {v['avg_view_pct']:.1f}%, "
            f"avg view duration {v['avg_view_duration']:.1f}s, engagement {v['engagement_rate']:.3f}"
            + (f", subs gained {v['subs_gained']}" if has_subscriber_data else "")
        )

    top_summary = "\n".join(f"- {summarize(v)}" for v in top_n)
    bottom_summary = "\n".join(f"- {summarize(v)}" for v in bottom_n) or "(not enough published videos yet)"
    pattern_summary = "\n".join(
        format_pattern_line(dim, data) for dim, data in patterns.items()
    )

    subscriber_note = (
        "Subscriber data is available - PRIMARY optimization signal has now shifted toward "
        "subscriber-conversion: weight subscriber-conversion patterns heavily in your recommendations, "
        "and call out which topics/hooks/lengths convert viewers into subscribers, not just which get views."
        if has_subscriber_data else
        "This channel has 0 subscribers so far and no subscriber data exists yet - use retention, "
        "engagement, and watch-time as the PRIMARY optimization signal while subscriber data remains "
        "sparse. Only fully shift priority to subscriber-conversion once enough subscriber-gaining "
        "videos exist to see a real pattern. Do not invent subscriber numbers or claims."
    )

    if competitor_trends:
        top_trend_lines = sorted(competitor_trends, key=lambda t: t["views"], reverse=True)[:10]
        trend_summary = "\n".join(
            f"- \"{t['title']}\" ({t['channel_title']}, {t['views']} views, "
            f"{t['duration_sec']}s) [{t['notes'] or 'no title-pattern flags'}]"
            for t in top_trend_lines
        )
    else:
        trend_summary = "(no external trend data collected this week)"

    return f"""You are the content strategist for MindByte, a cinematic psychology YouTube channel
(documentary-style narration over real B-roll, both Shorts and long-form video). Priority order:
1. Storytelling 2. Viewer retention 3. Emotional connection 4. Visual quality 5. Cinematic identity
6. Building a loyal returning audience 7. Automation/scale. The goal is a loyal audience and steady
subscriber growth, not just maximizing raw views.

This week's top-performing videos (composite score = rank-based blend of views/retention/duration/
engagement{"/subscriber conversion" if has_subscriber_data else ""}), across BOTH Shorts and long-form:
{top_summary}

This week's weakest-performing videos:
{bottom_summary}

Detected patterns across ALL videos in history (not just this week, both formats), recency-weighted
so recent performance counts more but nothing is decided from a single video. Each pattern carries a
confidence level (High/Medium/Low) - see the rule below:
{pattern_summary}

CONFIDENCE RULE: High = plenty of recent data AND a real, non-noise-level gap between best/worst.
Medium = some data but not enough for a strong claim, or a borderline gap. Low = too little data or
no meaningful gap - for Low confidence, do NOT assert a strategy change; say "insufficient data,
continue collecting" instead. EVERY recommendation you generate below (priority_actions_next_week,
topics_to_avoid, topics_to_increase, and each next_ideas item) MUST include its own "confidence" value
(High/Medium/Low), not just a global one.

{subscriber_note}

EXTERNAL INSPIRATION ONLY - top-performing videos from OTHER psychology-niche channels this week, via
the public YouTube Data API (real public data, NOT to be copied verbatim - use only for pattern
inspiration: title structure, length, framing):
{trend_summary}

Return ONLY valid JSON with this EXACT shape (no markdown, no extra keys):
{{
  "executive_summary": "<2-3 sentence plain-English summary of the week>",
  "biggest_wins": "<short paragraph>",
  "biggest_failures": "<short paragraph, specific about likely cause: hook, topic, pacing, length, upload time>",
  "viewer_behavior_analysis": "<short paragraph on how viewers actually behaved this week>",
  "retention_analysis": "<short paragraph specifically about retention/EarlyRetentionPct patterns>",
  "hook_analysis": "<short paragraph on which hook_type/hook_opener patterns worked>",
  "storytelling_analysis": "<short paragraph on structure/pacing patterns>",
  "subscriber_analysis": "<short paragraph - if no subscriber data yet, say so plainly and describe what proxy signals (retention, duration, engagement) are being used instead>",
  "competitor_trend_analysis": "<short paragraph on what the external inspiration data suggests, framed as inspiration only>",
  "priority_actions_next_week": [{{"action": "<concrete action>", "confidence": "High|Medium|Low"}}],
  "topics_to_avoid": [{{"topic": "<topic or pattern to avoid>", "confidence": "High|Medium|Low"}}],
  "topics_to_increase": [{{"topic": "<topic or pattern to lean into>", "confidence": "High|Medium|Low"}}],
  "recommended_recurring_series": "<short paragraph proposing 0-2 recurring series concepts, or say none yet>",
  "next_ideas": [
    {{
      "format": "short",
      "pillar": "<one of the 6 MindByte pillars>",
      "title": "<curiosity-driven working title>",
      "hook": "<one sentence describing the hook angle to use>",
      "hook_type": "<question|mystery|emotional|story>",
      "series": "<recurring series name, or empty string if none>",
      "angle": "<one sentence describing the story angle>",
      "seo_title": "<youtube-optimized title>",
      "seo_description": "<2-3 sentence description with hashtags>",
      "seo_tags": ["<tag1>", "<tag2>", "..."],
      "cta_style": "<what kind of closing/CTA to use>",
      "target_length_sec": 60,
      "thumbnail_concept": "<longform only - empty string for shorts>",
      "chapter_outline": ["<longform only - empty array for shorts>"],
      "loyalty_angle": "<short string: why this idea should make a viewer want to subscribe>",
      "confidence": "High|Medium|Low"
    }}
  ]
}}

Generate exactly {NEW_SHORTS_IDEAS_PER_WEEK} items with "format": "short" (target_length_sec between 45
and 75, thumbnail_concept "" and chapter_outline []) and exactly {NEW_LONGFORM_IDEAS_PER_WEEK} item(s)
with "format": "longform" (target_length_sec between 480 and 900, WITH a real thumbnail_concept and a
chapter_outline list of section titles), all building on what's actually working per the patterns
above, in the next_ideas array (total {NEW_SHORTS_IDEAS_PER_WEEK + NEW_LONGFORM_IDEAS_PER_WEEK} items)."""


def main() -> None:
    token = get_access_token()
    videos = load_videos(token)
    lf_videos = load_longform_videos(token)
    print(f"[weekly] {len(videos)} Shorts + {len(lf_videos)} long-form published/scheduled videos")
    all_videos = videos + lf_videos

    if not all_videos:
        print("[weekly] no published videos with analytics data yet - skipping plan generation")
        return

    history = load_analytics_history_latest(token)
    meta = load_video_meta(token)
    competitor_trends = load_competitor_trends(token)
    print(f"[weekly] {len(competitor_trends)} competitor-trend rows available as inspiration input")
    records = merge_records(all_videos, history, meta)
    compute_composite_scores(records)
    patterns, has_subscriber_data = detect_patterns(records)

    print(f"[weekly] subscriber data available: {has_subscriber_data}")
    for dim, data in patterns.items():
        print(f"[weekly] pattern - {format_pattern_line(dim, data)}")

    prompt = build_groq_prompt(records, patterns, has_subscriber_data, competitor_trends)
    try:
        raw = call_groq(prompt)
    except Exception as e:  # noqa: BLE001 - a Groq outage/non-429 error must not
        # crash the whole weekly run; skip this week's report and let next
        # Sunday's run try again, matching the non-fatal pattern used by
        # every other external call in this file (Sheets, YouTube, trends).
        print(f"[weekly] Groq call failed unexpectedly, skipping this week's report: {e}")
        return
    try:
        report = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[weekly] Groq did not return valid JSON ({e}); raw output:\n{raw}")
        return

    week_of = datetime.now(timezone.utc).date().isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()
    by_composite = sorted(records, key=lambda r: r["composite_score"], reverse=True)
    top_n = by_composite[:5]
    bottom_n = by_composite[-5:] if len(by_composite) > 5 else []

    pattern_summary_text = "; ".join(
        f"{dim}: best={data['best']['key'] if data['best'] else 'n/a'} [{data.get('confidence', 'Low')}]"
        for dim, data in patterns.items()
    )

    def _fmt_confidence_list(items: list, key: str) -> str:
        return "; ".join(f"{it.get(key, '')} [{it.get('confidence', 'Low')}]" for it in items) or "(none)"

    priority_actions = report.get("priority_actions_next_week", []) or []
    topics_to_avoid = report.get("topics_to_avoid", []) or []
    topics_to_increase = report.get("topics_to_increase", []) or []

    # WeeklyPlan: existing tab/columns, unchanged shape, just sourced from
    # the new report field names.
    plan_row = [
        week_of,
        " | ".join(f"{v['title']} ({v['views']}v)" for v in top_n),
        " | ".join(f"{v['title']} ({v['views']}v)" for v in bottom_n) if bottom_n else "",
        f"{report.get('executive_summary', '')} WINS: {report.get('biggest_wins', '')} "
        f"FAILURES: {report.get('biggest_failures', '')} "
        f"ACTIONS: {_fmt_confidence_list(priority_actions, 'action')}",
        pattern_summary_text,
        report.get("subscriber_analysis", ""),
        now_iso,
    ]
    append_with_selfheal(token, "WeeklyPlan", "WeeklyPlan!A:G", WEEKLY_PLAN_HEADER, plan_row)
    print("[weekly] WeeklyPlan row written")

    # WeeklyReportFull: one row per section per week, so a human can read
    # the entire structured report in the sheet.
    full_sections = {
        "executive_summary": report.get("executive_summary", ""),
        "biggest_wins": report.get("biggest_wins", ""),
        "biggest_failures": report.get("biggest_failures", ""),
        "viewer_behavior_analysis": report.get("viewer_behavior_analysis", ""),
        "retention_analysis": report.get("retention_analysis", ""),
        "hook_analysis": report.get("hook_analysis", ""),
        "storytelling_analysis": report.get("storytelling_analysis", ""),
        "subscriber_analysis": report.get("subscriber_analysis", ""),
        "competitor_trend_analysis": report.get("competitor_trend_analysis", ""),
        "priority_actions_next_week": _fmt_confidence_list(priority_actions, "action"),
        "topics_to_avoid": _fmt_confidence_list(topics_to_avoid, "topic"),
        "topics_to_increase": _fmt_confidence_list(topics_to_increase, "topic"),
        "recommended_recurring_series": report.get("recommended_recurring_series", ""),
        "pattern_summary": pattern_summary_text,
    }
    for section, content in full_sections.items():
        append_with_selfheal(
            token, WEEKLY_REPORT_FULL_TAB, f"{WEEKLY_REPORT_FULL_TAB}!A:C",
            WEEKLY_REPORT_FULL_HEADER, [week_of, section, content],
        )
    print(f"[weekly] {len(full_sections)} WeeklyReportFull section rows written")

    next_ideas = report.get("next_ideas", [])
    written = 0
    for idea in next_ideas:
        row = [
            week_of,
            idea.get("format", "short"),
            idea.get("pillar", ""),
            idea.get("title", ""),
            idea.get("hook", ""),
            idea.get("angle", ""),
            idea.get("seo_title", ""),
            idea.get("seo_description", ""),
            ", ".join(idea.get("seo_tags", []) or []),
            idea.get("cta_style", ""),
            idea.get("target_length_sec", ""),
            "N",
            now_iso,
            idea.get("hook_type", "unclassified"),
            idea.get("series", ""),
            idea.get("thumbnail_concept", ""),
            "; ".join(idea.get("chapter_outline", []) or []),
            idea.get("loyalty_angle", ""),
            idea.get("confidence", "Low"),
        ]
        append_with_selfheal(token, "NextWeekQueue", "NextWeekQueue!A:S", NEXT_QUEUE_HEADER, row)
        written += 1
    print(f"[weekly] {written} next-week content briefs written to NextWeekQueue")
    print("[weekly] done")


if __name__ == "__main__":
    main()
