"""
MindByte Automation - daily analytics sync.

Pulls current views/likes/comments/shares for every video logged in the
Videos sheet and writes the latest numbers back, so the channel owner never
needs to open YouTube Studio.

Extended 2026-07-30 for the weekly self-improving system:
  - Also pulls averageViewDuration, averageViewPercentage (retention), and
    subscribersGained per video from the YouTube Analytics API (batched
    into the same call that already fetched "shares", so this adds zero
    extra API calls per video).
  - Appends a permanent daily snapshot row per video to a new
    "AnalyticsHistory" tab, instead of only overwriting the Videos tab's
    single "latest" row - this is what lets weekly_review.py track growth
    curves and long-term trends across months, not just a point-in-time
    snapshot.
  - Everything here is additive: the existing Videos!I:M write is
    unchanged (same columns, same meaning), so any script or person
    reading that tab today keeps working exactly as before.
"""

import os
from datetime import datetime, timezone

import requests

OAUTH_CLIENT_ID = os.environ["OAUTH_CLIENT_ID"]
OAUTH_CLIENT_SECRET = os.environ["OAUTH_CLIENT_SECRET"]
OAUTH_REFRESH_TOKEN = os.environ["OAUTH_REFRESH_TOKEN"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
YOUTUBE_CHANNEL_ID = os.environ["YOUTUBE_CHANNEL_ID"]

SHEETS_BASE = f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_SHEET_ID}"
SESSION = requests.Session()

ANALYTICS_HISTORY_TAB = "AnalyticsHistory"
ANALYTICS_HISTORY_HEADER = [
    "Date", "VideoID", "Title", "Topic", "Pillar", "Format",
    "Views", "Likes", "Comments", "Shares",
    "AvgViewDurationSec", "AvgViewPercentage", "SubscribersGained",
    "UploadHourUTC", "PublishDate",
]


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


def get_video_stats(token: str, video_id: str) -> dict:
    resp = SESSION.get(
        "https://www.googleapis.com/youtube/v3/videos",
        headers=headers(token),
        params={"part": "statistics", "id": video_id},
        timeout=30,
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    if not items:
        return {}
    return items[0].get("statistics", {})


def get_video_analytics(token: str, video_id: str) -> dict:
    """Pulls shares, average view duration, average view percentage
    (retention), and subscribers gained for one video in a SINGLE
    YouTube Analytics API call (all requested via one comma-separated
    `metrics` param) - this used to be a shares-only call; batching the
    extra metrics into the same request keeps this at one Analytics API
    call per video, not four. Falls back to all-zero on any failure
    (e.g. video too new for Analytics data to have landed yet) rather
    than ever raising, since this must never break the daily sync."""
    resp = SESSION.get(
        "https://youtubeanalytics.googleapis.com/v2/reports",
        headers=headers(token),
        params={
            "ids": f"channel=={YOUTUBE_CHANNEL_ID}",
            "startDate": "2020-01-01",
            "endDate": datetime.now(timezone.utc).date().isoformat(),
            "metrics": "shares,averageViewDuration,averageViewPercentage,subscribersGained",
            "filters": f"video=={video_id}",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        return {"shares": 0, "avg_view_duration": 0, "avg_view_pct": 0.0, "subs_gained": 0}
    rows = resp.json().get("rows", [])
    if not rows:
        return {"shares": 0, "avg_view_duration": 0, "avg_view_pct": 0.0, "subs_gained": 0}
    row = rows[0]
    return {
        "shares": int(row[0]) if len(row) > 0 else 0,
        "avg_view_duration": float(row[1]) if len(row) > 1 else 0,
        "avg_view_pct": float(row[2]) if len(row) > 2 else 0.0,
        "subs_gained": int(row[3]) if len(row) > 3 else 0,
    }


def sheet_get(token: str, a1_range: str) -> list:
    resp = SESSION.get(
        f"{SHEETS_BASE}/values/{a1_range}",
        headers=headers(token),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("values", [])


def sheet_update(token: str, a1_range: str, row: list) -> None:
    resp = SESSION.put(
        f"{SHEETS_BASE}/values/{a1_range}",
        headers=headers(token),
        params={"valueInputOption": "USER_ENTERED"},
        json={"values": [row]},
        timeout=30,
    )
    resp.raise_for_status()


def sheet_append(token: str, a1_range: str, row: list) -> None:
    resp = SESSION.post(
        f"{SHEETS_BASE}/values/{a1_range}:append",
        headers=headers(token),
        params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
        json={"values": [row]},
        timeout=30,
    )
    resp.raise_for_status()


def ensure_sheet_tab(token: str, tab_name: str, header_row: list) -> bool:
    """Same self-heal pattern used throughout pipeline.py: create the tab
    (with a header row) the first time a write to it fails because it
    doesn't exist yet, then let the caller retry once."""
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


def append_history_row(token: str, row: list) -> None:
    try:
        sheet_append(token, f"{ANALYTICS_HISTORY_TAB}!A:O", row)
    except Exception as e:  # noqa: BLE001 - history logging must never crash the sync
        healed = False
        try:
            healed = ensure_sheet_tab(token, ANALYTICS_HISTORY_TAB, ANALYTICS_HISTORY_HEADER)
            if healed:
                sheet_append(token, f"{ANALYTICS_HISTORY_TAB}!A:O", row)
        except Exception:
            healed = False
        if not healed:
            print(f"[analytics] could not log to {ANALYTICS_HISTORY_TAB} tab (does it exist yet?): {e}")


def load_video_meta(token: str) -> dict:
    """Best-effort load of the VideoMeta tab (written by pipeline.py/
    pipeline_longform.py at publish time) keyed by video_id, so history
    rows can carry pillar/format/upload-hour even though the Videos tab
    itself doesn't store those fields. Returns {} on any failure (e.g.
    the tab doesn't exist yet) - this is enrichment only, never required."""
    try:
        rows = sheet_get(token, "VideoMeta!A2:N")
    except Exception:
        return {}
    meta = {}
    for row in rows:
        row = row + [""] * (14 - len(row))
        video_id = row[0].strip()
        if not video_id:
            continue
        meta[video_id] = {
            "pillar": row[3], "format": row[4], "upload_hour_utc": row[11],
        }
    return meta


def main() -> None:
    token = get_access_token()
    rows = sheet_get(token, "Videos!A2:O")
    print(f"[analytics] {len(rows)} rows in Videos sheet")
    video_meta = load_video_meta(token)
    today = datetime.now(timezone.utc).date().isoformat()

    for i, row in enumerate(rows, start=2):  # sheet row 2 is the first data row
        row = row + [""] * (15 - len(row))
        video_id = row[0].strip()
        title, topic = row[1], row[2]
        publish_date = row[4]
        if not video_id:
            continue
        stats = get_video_stats(token, video_id)
        if not stats:
            print(f"[analytics] row {i}: video {video_id} not found (may still be private)")
            continue
        analytics = get_video_analytics(token, video_id)
        views = stats.get("viewCount", 0)
        likes = stats.get("likeCount", 0)
        comments = stats.get("commentCount", 0)
        shares = analytics["shares"]
        now = datetime.now(timezone.utc).isoformat()

        # Columns I-M are Views, Likes, Comments, Shares, Last Synced -
        # unchanged from before, so anything reading the Videos tab today
        # keeps working exactly as before.
        sheet_update(token, f"Videos!I{i}:M{i}", [views, likes, comments, shares, now])
        print(f"[analytics] row {i}: video {video_id} -> views={views} likes={likes} "
              f"comments={comments} shares={shares} avg_view_pct={analytics['avg_view_pct']:.1f} "
              f"avg_view_duration={analytics['avg_view_duration']:.1f}s subs_gained={analytics['subs_gained']}")

        meta = video_meta.get(video_id, {})
        append_history_row(token, [
            today, video_id, title, topic, meta.get("pillar", ""), meta.get("format", ""),
            views, likes, comments, shares,
            round(analytics["avg_view_duration"], 1), round(analytics["avg_view_pct"], 2),
            analytics["subs_gained"], meta.get("upload_hour_utc", ""), publish_date,
        ])

    print("[analytics] done")


if __name__ == "__main__":
    main()
