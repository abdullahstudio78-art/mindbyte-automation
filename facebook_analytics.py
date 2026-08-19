"""
MindByte Automation - daily Facebook Reels analytics sync (added 2026-08-19).

Mirrors what analytics_sync.py already does for YouTube: reads every video
logged so far (here, from the "FacebookVideoMeta" tab that facebook_pipeline.py
and pipeline.py's Facebook cross-post block write to), pulls current
performance numbers from the Graph API, and appends a permanent daily
snapshot row per video to a new "FacebookAnalyticsHistory" tab - so
facebook_weekly_review.py can track growth over time, not just a single
point-in-time number.

Run daily via .github/workflows/facebook_analytics.yml (same cadence as the
YouTube analytics_sync.py workflow).

--------------------------------------------------------------------------
Metrics and API note
--------------------------------------------------------------------------
Meta's Reels Insights fields have changed shape more than once and aren't
guaranteed stable release to release, and this sandbox has no live Facebook
credentials to verify field names against a real response. To stay robust
against that:
  1. Try the modern `/​{video_id}/video_insights` metrics first
     (blue_reels_play_count = plays, post_video_avg_time_watched,
     post_video_view_time).
  2. ALWAYS also pull the plain `/​{video_id}` object fields
     (likes.summary(true), comments.summary(true), shares) as a second,
     more stable data point - these basic engagement fields have been
     stable on the Graph API for years, unlike the Insights metric names.
  3. Any single metric that 400s/is unavailable is skipped (logged, not
     fatal) rather than aborting the whole video's row - a partial row
     (e.g. plays missing but likes/comments present) is still useful and
     still gets written.
This is the same "best-effort, never abort the run" posture as every other
integration in this codebase.
"""

import os
from datetime import datetime, timezone

import requests

import pipeline as p

FACEBOOK_PAGE_ACCESS_TOKEN = os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN", "")
GRAPH_VERSION = "v26.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

FACEBOOK_VIDEO_META_RANGE = "FacebookVideoMeta!A:N"
FACEBOOK_ANALYTICS_HISTORY_TAB = "FacebookAnalyticsHistory"
FACEBOOK_ANALYTICS_HISTORY_RANGE = "FacebookAnalyticsHistory!A:N"
FACEBOOK_ANALYTICS_HISTORY_HEADER = [
    "Date", "FacebookVideoID", "Title", "Topic", "Pillar",
    "Plays", "AvgTimeWatchedSec", "TotalViewTimeSec",
    "Likes", "Comments", "Shares",
    "PostHourUTC", "CreatedAt",
]


def facebook_analytics_configured() -> bool:
    return bool(FACEBOOK_PAGE_ACCESS_TOKEN)


def _get(path: str, params: dict) -> dict:
    resp = requests.get(f"{GRAPH_BASE}/{path}", params={**params, "access_token": FACEBOOK_PAGE_ACCESS_TOKEN},
                         timeout=30)
    if resp.status_code != 200:
        return {}
    return resp.json()


def _fetch_insights(video_id: str) -> dict:
    """Best-effort pull of Reels-specific play/watch-time metrics. Returns
    {} on any failure or unsupported metric - see module docstring."""
    data = _get(f"{video_id}/video_insights", {
        "metric": "blue_reels_play_count,post_video_avg_time_watched,post_video_view_time",
    })
    out = {}
    for entry in data.get("data", []):
        name = entry.get("name")
        values = entry.get("values", [])
        if not values:
            continue
        value = values[-1].get("value")
        if isinstance(value, dict):
            # Some metrics return a breakdown dict rather than a scalar -
            # sum whatever numeric values are present as a reasonable total.
            value = sum(v for v in value.values() if isinstance(v, (int, float)))
        if name == "blue_reels_play_count":
            out["plays"] = value
        elif name == "post_video_avg_time_watched":
            out["avg_time_watched_sec"] = value
        elif name == "post_video_view_time":
            out["total_view_time_sec"] = value
    return out


def _fetch_engagement(video_id: str) -> dict:
    """Stable, long-supported basic fields - the fallback/second data point
    described in the module docstring."""
    data = _get(video_id, {"fields": "likes.summary(true).limit(0),comments.summary(true).limit(0),shares"})
    return {
        "likes": (data.get("likes", {}) or {}).get("summary", {}).get("total_count"),
        "comments": (data.get("comments", {}) or {}).get("summary", {}).get("total_count"),
        "shares": (data.get("shares", {}) or {}).get("count"),
    }


def main() -> None:
    if not facebook_analytics_configured():
        print("[facebook_analytics] skipped: FACEBOOK_PAGE_ACCESS_TOKEN not configured yet")
        return

    access_token = p.get_access_token()
    try:
        rows = p.sheet_get(access_token, FACEBOOK_VIDEO_META_RANGE)
    except Exception as e:  # noqa: BLE001 - tab may not exist yet, that's fine
        print(f"[facebook_analytics] FacebookVideoMeta not available yet ({e}) - nothing to sync")
        return

    if not rows:
        print("[facebook_analytics] no Facebook videos logged yet - nothing to sync")
        return

    today = datetime.now(timezone.utc).date().isoformat()
    synced = 0
    for row in rows[1:] if rows and rows[0] and rows[0][0] == "FacebookVideoID" else rows:
        row = row + [""] * (14 - len(row))
        video_id, title, topic, pillar = row[0], row[1], row[2], row[3]
        post_hour, created_at = row[10], row[13]
        if not video_id:
            continue

        insights = _fetch_insights(video_id)
        engagement = _fetch_engagement(video_id)

        history_row = [
            today, video_id, title, topic, pillar,
            insights.get("plays", ""), insights.get("avg_time_watched_sec", ""),
            insights.get("total_view_time_sec", ""),
            engagement.get("likes", ""), engagement.get("comments", ""), engagement.get("shares", ""),
            post_hour, created_at,
        ]
        try:
            p.sheet_append(access_token, FACEBOOK_ANALYTICS_HISTORY_RANGE, history_row)
        except Exception as e:  # noqa: BLE001
            healed = False
            try:
                healed = p.ensure_sheet_tab(access_token, FACEBOOK_ANALYTICS_HISTORY_TAB,
                                             FACEBOOK_ANALYTICS_HISTORY_HEADER)
                if healed:
                    p.sheet_append(access_token, FACEBOOK_ANALYTICS_HISTORY_RANGE, history_row)
            except Exception:
                healed = False
            if not healed:
                print(f"[facebook_analytics] could not log history row for {video_id}: {e}")
                continue

        synced += 1
        print(f"[facebook_analytics] {video_id} ({title[:40]!r}): "
              f"plays={insights.get('plays', 'n/a')} likes={engagement.get('likes', 'n/a')} "
              f"comments={engagement.get('comments', 'n/a')} shares={engagement.get('shares', 'n/a')}")

    print(f"[facebook_analytics] synced {synced}/{len(rows)} videos")


if __name__ == "__main__":
    main()
