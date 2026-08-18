"""
MindByte Automation - external/competitor trend learning (2026-07-31).

Fully optional, best-effort script. Reuses the EXISTING Google OAuth
refresh-token flow (same env vars / get_access_token() pattern as
analytics_sync.py - no new secrets) to call the PUBLIC YouTube Data API v3:
  1. search.list (part=snippet, type=video, order=viewCount, published in
     the last 30 days) for a small rotating list of psychology-niche
     queries.
  2. videos.list (part=statistics,contentDetails,snippet) on the resulting
     video IDs to get view counts, duration, title, channel title, and
     published date.

This is REAL public data pulled via the same YouTube Data API this repo
already has credentials for - NOT scraping, NOT fabricated, NOT copied
content. It computes only lightweight, defensible pattern summaries (video
length distribution among top results, simple title-pattern heuristics via
regex - question marks / numbers / colons - nothing claimed as NLP), plus
the raw (title, channel, views, length) list as inspiration examples.

Results are written to a new self-healing CompetitorTrends tab and are
intended to feed weekly_review.py's Groq prompt as "external inspiration,
not to be copied - adapt patterns only" - this script does NOT write any
code that copies or reproduces competitor titles/scripts into MindByte's
own generation.

The entire script is wrapped in a top-level try/except: quota exhaustion,
zero results, or any API error must exit cleanly with a log message and
zero rows written, never fail a workflow run it's part of.
"""

import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests

OAUTH_CLIENT_ID = os.environ.get("OAUTH_CLIENT_ID", "")
OAUTH_CLIENT_SECRET = os.environ.get("OAUTH_CLIENT_SECRET", "")
OAUTH_REFRESH_TOKEN = os.environ.get("OAUTH_REFRESH_TOKEN", "")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")

SHEETS_BASE = f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_SHEET_ID}"
SESSION = requests.Session()

COMPETITOR_TRENDS_TAB = "CompetitorTrends"
COMPETITOR_TRENDS_HEADER = [
    "Date", "Query", "Title", "ChannelTitle", "Views", "DurationSec", "PublishedAt", "Notes",
]

NICHE_QUERIES = [
    "psychology facts",
    "human behavior explained",
    "relationship psychology",
    "why do people",
    "psychology of the mind",
]

MAX_RESULTS_PER_QUERY = 15
LOOKBACK_DAYS = 30


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


RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


def _api_call_with_retry(fn, _retries: int = 2, _label: str = "API"):
    """Same 429/5xx retry-with-backoff pattern added to pipeline.py and
    analytics_sync.py on 2026-08-19. This script already degrades to
    "zero rows written" on any failure, so this isn't about preventing a
    crash - it's about not silently discarding real competitor-trend data
    over a transient rate limit that a short retry would have recovered
    from."""
    last_exc = None
    for attempt in range(_retries + 1):
        try:
            resp = fn()
        except requests.exceptions.RequestException as e:
            last_exc = e
            if attempt < _retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
        if resp.status_code in RETRYABLE_STATUSES and attempt < _retries:
            wait_s = 1.5 * (attempt + 1)
            print(f"[external_trends] {_label} call got {resp.status_code} - retrying in {wait_s:.1f}s")
            time.sleep(wait_s)
            continue
        return resp
    if last_exc:
        raise last_exc
    return resp


def _iso8601_duration_to_seconds(duration: str) -> int:
    """Parses YouTube's ISO-8601 duration format (e.g. 'PT4M13S') into
    seconds. Returns 0 on anything unparseable - never raises."""
    try:
        match = re.match(
            r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration or "",
        )
        if not match:
            return 0
        h, m, s = (int(g) if g else 0 for g in match.groups())
        return h * 3600 + m * 60 + s
    except Exception:
        return 0


def search_top_videos(token: str, query: str) -> list:
    """search.list -> videos.list for one query. Returns a list of dicts
    (title, channel_title, views, duration_sec, published_at). Any failure
    (quota, network, empty results) returns an empty list rather than
    raising - this is best-effort inspiration data, never required."""
    published_after = (
        datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    ).isoformat().replace("+00:00", "Z")
    try:
        search_resp = _api_call_with_retry(
            lambda: SESSION.get(
                "https://www.googleapis.com/youtube/v3/search",
                headers=headers(token),
                params={
                    "part": "snippet",
                    "type": "video",
                    "q": query,
                    "order": "viewCount",
                    "publishedAfter": published_after,
                    "maxResults": MAX_RESULTS_PER_QUERY,
                },
                timeout=30,
            ),
            _label="search.list",
        )
        if search_resp.status_code != 200:
            print(f"[external_trends] search failed for '{query}': {search_resp.status_code}")
            return []
        items = search_resp.json().get("items", [])
        video_ids = [it["id"]["videoId"] for it in items if it.get("id", {}).get("videoId")]
        if not video_ids:
            return []

        videos_resp = _api_call_with_retry(
            lambda: SESSION.get(
                "https://www.googleapis.com/youtube/v3/videos",
                headers=headers(token),
                params={
                    "part": "statistics,contentDetails,snippet",
                    "id": ",".join(video_ids),
                },
                timeout=30,
            ),
            _label="videos.list",
        )
        if videos_resp.status_code != 200:
            print(f"[external_trends] videos.list failed for '{query}': {videos_resp.status_code}")
            return []
        results = []
        for item in videos_resp.json().get("items", []):
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})
            content = item.get("contentDetails", {})
            results.append({
                "title": snippet.get("title", ""),
                "channel_title": snippet.get("channelTitle", ""),
                "views": int(stats.get("viewCount", 0)),
                "duration_sec": _iso8601_duration_to_seconds(content.get("duration", "")),
                "published_at": snippet.get("publishedAt", ""),
            })
        return results
    except Exception as e:  # noqa: BLE001 - one bad query must not stop the whole run
        print(f"[external_trends] query '{query}' failed unexpectedly (non-fatal): {e}")
        return []


def title_pattern_notes(title: str) -> str:
    """Simple regex heuristics only - not NLP claims - flagging surface
    title patterns (question marks, numbers, colons) that showed up in a
    top-performing result, for lightweight pattern-inspiration only."""
    flags = []
    if "?" in title:
        flags.append("question")
    if re.search(r"\d", title):
        flags.append("number")
    if ":" in title:
        flags.append("colon")
    return ", ".join(flags) if flags else ""


def sheet_append(token: str, a1_range: str, row: list) -> None:
    resp = _api_call_with_retry(
        lambda: SESSION.post(
            f"{SHEETS_BASE}/values/{a1_range}:append",
            headers=headers(token),
            params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
            json={"values": [row]},
            timeout=30,
        ),
        _label="Sheets",
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


def append_with_selfheal(token: str, row: list) -> bool:
    try:
        sheet_append(token, f"{COMPETITOR_TRENDS_TAB}!A:H", row)
        return True
    except Exception:
        if ensure_sheet_tab(token, COMPETITOR_TRENDS_TAB, COMPETITOR_TRENDS_HEADER):
            try:
                sheet_append(token, f"{COMPETITOR_TRENDS_TAB}!A:H", row)
                return True
            except Exception:
                return False
        return False


def run() -> None:
    if not (OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET and OAUTH_REFRESH_TOKEN and GOOGLE_SHEET_ID):
        print("[external_trends] required OAuth/Sheet env vars missing - skipping run")
        return

    token = get_access_token()
    today = datetime.now(timezone.utc).date().isoformat()
    rows_written = 0
    all_results = []

    for query in NICHE_QUERIES:
        results = search_top_videos(token, query)
        print(f"[external_trends] query '{query}': {len(results)} results")
        for r in results:
            all_results.append(r)
            notes = title_pattern_notes(r["title"])
            row = [
                today, query, r["title"], r["channel_title"], r["views"],
                r["duration_sec"], r["published_at"], notes,
            ]
            if append_with_selfheal(token, row):
                rows_written += 1

    if all_results:
        lengths = [r["duration_sec"] for r in all_results if r["duration_sec"] > 0]
        avg_len = sum(lengths) / len(lengths) if lengths else 0
        q_count = sum(1 for r in all_results if "?" in r["title"])
        num_count = sum(1 for r in all_results if re.search(r"\d", r["title"]))
        colon_count = sum(1 for r in all_results if ":" in r["title"])
        print(
            f"[external_trends] summary: n={len(all_results)} avg_duration_sec={avg_len:.0f} "
            f"titles_with_question_mark={q_count} titles_with_number={num_count} "
            f"titles_with_colon={colon_count}"
        )

    print(f"[external_trends] done - {rows_written} rows written to {COMPETITOR_TRENDS_TAB}")


def main() -> None:
    """Top-level try/except: ANY failure (quota exceeded, no results, API
    error, missing credentials) must exit cleanly with a log message and
    zero rows written - this script must never fail a workflow run it's
    part of."""
    try:
        run()
    except Exception as e:  # noqa: BLE001 - this script must never fail its workflow run
        print(f"[external_trends] run failed unexpectedly (non-fatal, exiting cleanly): {e}")


if __name__ == "__main__":
    main()
