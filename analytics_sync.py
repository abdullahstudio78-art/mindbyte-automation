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

Extended 2026-07-31 for long-form parity + external-signal helpers:
  - A SECOND loop now reads the LongformVideos tab (written by
    pipeline_longform.py) and pulls the same YouTube Data/Analytics stats
    per long-form video, appending Format="longform" rows to the SAME
    AnalyticsHistory tab the Shorts loop already writes to, and appending
    new Views/Likes/Comments/Shares/LastSynced columns onto the END of the
    LongformVideos tab itself (that tab never reserved columns for these,
    unlike Videos!I:M, so this is a pure append rather than an overwrite).
  - IMPORTANT API LIMITATION (documented here rather than guessed around):
    YouTube Shorts do NOT expose a true impressions/CTR metric via the
    public YouTube Analytics API the way long-form does - Shorts discovery
    is swipe/feed-based, not thumbnail-click-based, and the API simply has
    no equivalent field for Shorts. This code does NOT fabricate an
    impressions/CTR number for Shorts. Instead, EarlyRetentionPct (retention
    in the first few seconds of the video, from an elapsedVideoTimeRatio
    report) is used as the closest reliable proxy for "does the opening
    grab people" that the public API actually supports for both formats.
  - EarlyRetentionPct is a best-effort, may-be-None value: for Shorts it is
    the audienceWatchRatio at the first available elapsedVideoTimeRatio
    bucket (a proxy for "the first ~3 seconds"). For long-form it uses the
    bucket closest to ratio 0.05 as a proxy for "the first ~30 seconds" of
    a roughly 10-minute video - this is an APPROXIMATION, since
    elapsedVideoTimeRatio is a relative (0.0-1.0) position in the video,
    not an absolute second count, and actual video length varies. Treat it
    as directional, not a precise seconds-based figure.
  - A channel-level (not per-video, to avoid quota multiplication)
    subscribedStatus report is pulled once per sync run as a best-effort
    "returning viewers" signal, logged and written to a new
    ChannelAudienceSnapshot tab.
"""

import os
import time
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
    "EarlyRetentionPct",  # appended at end 2026-07-31 - see module docstring
]

# New end-of-header columns appended to LongformVideos (2026-07-31) - that
# tab never reserved view/like/comment columns the way Videos!I:M did, so
# these are appended after whatever the last existing column is rather than
# assuming a fixed position.
LONGFORM_NEW_COLS = ["Views", "Likes", "Comments", "Shares", "LastSynced"]

CHANNEL_AUDIENCE_TAB = "ChannelAudienceSnapshot"
CHANNEL_AUDIENCE_HEADER = ["Date", "SubscribedViews", "UnsubscribedViews", "SubscribedSharePct"]

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


def _api_call_with_retry(fn, _retries: int = 2, _label: str = "API"):
    """Same 429/5xx retry-with-backoff idea already used for Groq calls
    (call_groq(..., _retries=2) in pipeline.py/weekly_review.py): catch a
    rate-limit/server error, sleep with a short backoff, retry up to
    `_retries` times, then let the final exception propagate as before.

    2026-08-19: generalized from the Sheets-only `_sheets_call_with_retry`
    to cover the YouTube Data/Analytics API calls too
    (get_video_stats/get_video_analytics/get_early_retention_pct/
    get_channel_audience_snapshot) - those run once (or twice, with the
    retention-curve call) per video in a loop, so they're the highest-
    volume, most rate-limit-exposed calls in this file, and previously had
    ZERO retry protection: a transient 429/500 there just silently
    degraded to zero/None data instead of being recovered, quietly
    understating real analytics rather than actually failing loudly. Named
    generically now since it's used for both Sheets and YouTube API
    calls."""
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
        if resp.status_code in RETRYABLE_STATUSES and attempt < _retries:
            wait_s = 1.5 * (attempt + 1)
            print(f"[analytics] {_label} call got {resp.status_code} - retrying in {wait_s:.1f}s")
            time.sleep(wait_s)
            continue
        return resp
    if last_exc:
        raise last_exc
    return resp


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
    resp = _api_call_with_retry(
        lambda: SESSION.get(
            "https://www.googleapis.com/youtube/v3/videos",
            headers=headers(token),
            params={"part": "statistics", "id": video_id},
            timeout=30,
        ),
        _label="YouTube Data API (video stats)",
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
    resp = _api_call_with_retry(
        lambda: SESSION.get(
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
        ),
        _label="YouTube Analytics API (video metrics)",
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


def get_early_retention_pct(token: str, video_id: str, is_longform: bool) -> float | None:
    """Best-effort retention-curve summary: a SEPARATE YouTube Analytics API
    v2 report per video, dimensioned by elapsedVideoTimeRatio (metrics=
    audienceWatchRatio). Returns one summary number - see the module
    docstring for the exact definition/approximation for each format.
    Wrapped end-to-end in try/except: missing data is very likely for
    low-view videos, and a None here must never block the row write."""
    try:
        resp = _api_call_with_retry(
            lambda: SESSION.get(
                "https://youtubeanalytics.googleapis.com/v2/reports",
                headers=headers(token),
                params={
                    "ids": f"channel=={YOUTUBE_CHANNEL_ID}",
                    "startDate": "2020-01-01",
                    "endDate": datetime.now(timezone.utc).date().isoformat(),
                    "metrics": "audienceWatchRatio",
                    "dimensions": "elapsedVideoTimeRatio",
                    "filters": f"video=={video_id}",
                    "sort": "elapsedVideoTimeRatio",
                },
                timeout=30,
            ),
            _label="YouTube Analytics API (retention curve)",
        )
        if resp.status_code != 200:
            return None
        rows = resp.json().get("rows", [])
        if not rows:
            return None
        if not is_longform:
            # Shorts: first available bucket is the closest proxy for
            # "the first ~3 seconds" since Shorts are already only 30-75s.
            ratio, watch_ratio = rows[0][0], rows[0][1]
            return round(float(watch_ratio) * 100, 2)
        # Long-form: bucket closest to ratio 0.05 (APPROXIMATION of "first
        # ~30 seconds" on a ~10 minute video - elapsedVideoTimeRatio is
        # relative, not absolute seconds, so this is directional only).
        best_row = min(rows, key=lambda r: abs(float(r[0]) - 0.05))
        return round(float(best_row[1]) * 100, 2)
    except Exception as e:  # noqa: BLE001 - retention-curve data is a bonus, never fatal
        print(f"[analytics] EarlyRetentionPct lookup failed for {video_id} (non-fatal): {e}")
        return None


def get_channel_audience_snapshot(token: str) -> dict | None:
    """Best-effort, once-per-run (NOT per-video, to avoid quota
    multiplication) channel-level subscribedStatus report - a simple
    "returning viewers" proxy signal. Returns None on any failure."""
    try:
        resp = _api_call_with_retry(
            lambda: SESSION.get(
                "https://youtubeanalytics.googleapis.com/v2/reports",
                headers=headers(token),
                params={
                    "ids": f"channel=={YOUTUBE_CHANNEL_ID}",
                    "startDate": "2020-01-01",
                    "endDate": datetime.now(timezone.utc).date().isoformat(),
                    "metrics": "views",
                    "dimensions": "subscribedStatus",
                },
                timeout=30,
            ),
            _label="YouTube Analytics API (channel audience)",
        )
        if resp.status_code != 200:
            return None
        rows = resp.json().get("rows", [])
        if not rows:
            return None
        subscribed = 0
        unsubscribed = 0
        for row in rows:
            status, views = row[0], int(row[1])
            if status == "SUBSCRIBED":
                subscribed = views
            elif status == "UNSUBSCRIBED":
                unsubscribed = views
        total = subscribed + unsubscribed
        share_pct = round(subscribed / total * 100, 2) if total > 0 else 0.0
        return {"subscribed": subscribed, "unsubscribed": unsubscribed, "share_pct": share_pct}
    except Exception as e:  # noqa: BLE001 - best-effort signal only, never fatal
        print(f"[analytics] channel audience snapshot failed (non-fatal): {e}")
        return None


def sheet_get(token: str, a1_range: str) -> list:
    resp = _api_call_with_retry(
        lambda: SESSION.get(f"{SHEETS_BASE}/values/{a1_range}", headers=headers(token), timeout=30),
        _label="Sheets",
    )
    resp.raise_for_status()
    return resp.json().get("values", [])


def sheet_update(token: str, a1_range: str, row: list) -> None:
    resp = _api_call_with_retry(
        lambda: SESSION.put(
            f"{SHEETS_BASE}/values/{a1_range}",
            headers=headers(token),
            params={"valueInputOption": "USER_ENTERED"},
            json={"values": [row]},
            timeout=30,
        ),
        _label="Sheets",
    )
    resp.raise_for_status()


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
        sheet_append(token, f"{ANALYTICS_HISTORY_TAB}!A:P", row)
    except Exception as e:  # noqa: BLE001 - history logging must never crash the sync
        healed = False
        try:
            healed = ensure_sheet_tab(token, ANALYTICS_HISTORY_TAB, ANALYTICS_HISTORY_HEADER)
            if healed:
                sheet_append(token, f"{ANALYTICS_HISTORY_TAB}!A:P", row)
        except Exception:
            healed = False
        if not healed:
            print(f"[analytics] could not log to {ANALYTICS_HISTORY_TAB} tab (does it exist yet?): {e}")


def append_channel_audience_snapshot(token: str, snapshot: dict) -> None:
    """Best-effort single row per sync run into the new, self-healing
    ChannelAudienceSnapshot tab. Never raises."""
    row = [
        datetime.now(timezone.utc).date().isoformat(),
        snapshot["subscribed"], snapshot["unsubscribed"], snapshot["share_pct"],
    ]
    try:
        sheet_append(token, f"{CHANNEL_AUDIENCE_TAB}!A:D", row)
    except Exception as e:  # noqa: BLE001 - never crash the sync over this
        healed = False
        try:
            healed = ensure_sheet_tab(token, CHANNEL_AUDIENCE_TAB, CHANNEL_AUDIENCE_HEADER)
            if healed:
                sheet_append(token, f"{CHANNEL_AUDIENCE_TAB}!A:D", row)
        except Exception:
            healed = False
        if not healed:
            print(f"[analytics] could not log to {CHANNEL_AUDIENCE_TAB} tab (non-fatal): {e}")


def sync_longform_videos(token: str, video_meta: dict, today: str) -> None:
    """Long-form parity loop (2026-07-31): pulls the same YouTube Data/
    Analytics stats for every LongformVideos row and (a) appends new
    Views/Likes/Comments/Shares/LastSynced columns at the END of that
    tab's header, and (b) appends an enriched AnalyticsHistory row with
    Format="longform", exactly like the Shorts loop above. Wrapped so any
    failure to even read the LongformVideos tab (e.g. it doesn't exist yet)
    degrades to a no-op rather than breaking the Shorts sync that already
    ran successfully above."""
    try:
        rows = sheet_get(token, "LongformVideos!A2:N")
    except Exception as e:  # noqa: BLE001 - tab may not exist yet; that's fine
        print(f"[analytics] LongformVideos tab not available yet (non-fatal): {e}")
        return
    print(f"[analytics] {len(rows)} rows in LongformVideos sheet")

    for i, row in enumerate(rows, start=2):
        row = row + [""] * (14 - len(row))
        video_id = row[0].strip()
        title, topic = row[1], row[2]
        publish_date = row[4]
        if not video_id:
            continue
        try:
            stats = get_video_stats(token, video_id)
        except Exception as e:  # noqa: BLE001 - one bad video must not stop the loop
            print(f"[analytics] LongformVideos row {i}: stats lookup failed (non-fatal): {e}")
            continue
        if not stats:
            print(f"[analytics] LongformVideos row {i}: video {video_id} not found (may still be private)")
            continue
        analytics = get_video_analytics(token, video_id)
        views = stats.get("viewCount", 0)
        likes = stats.get("likeCount", 0)
        comments = stats.get("commentCount", 0)
        shares = analytics["shares"]
        now = datetime.now(timezone.utc).isoformat()

        # New end-of-header columns O-S (Views, Likes, Comments, Shares,
        # LastSynced) - the existing A-N columns (VideoID..Notes) are
        # completely untouched.
        try:
            sheet_update(token, f"LongformVideos!O{i}:S{i}", [views, likes, comments, shares, now])
        except Exception as e:  # noqa: BLE001 - a write failure shouldn't stop the loop
            print(f"[analytics] LongformVideos row {i}: could not write new columns (non-fatal): {e}")

        early_retention = get_early_retention_pct(token, video_id, is_longform=True)
        print(f"[analytics] LongformVideos row {i}: video {video_id} -> views={views} likes={likes} "
              f"comments={comments} shares={shares} avg_view_pct={analytics['avg_view_pct']:.1f} "
              f"early_retention_pct={early_retention}")

        meta = video_meta.get(video_id, {})
        append_history_row(token, [
            today, video_id, title, topic, meta.get("pillar", ""), "longform",
            views, likes, comments, shares,
            round(analytics["avg_view_duration"], 1), round(analytics["avg_view_pct"], 2),
            analytics["subs_gained"], meta.get("upload_hour_utc", ""), publish_date,
            early_retention,
        ])


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

        early_retention = get_early_retention_pct(token, video_id, is_longform=False)
        meta = video_meta.get(video_id, {})
        append_history_row(token, [
            today, video_id, title, topic, meta.get("pillar", ""), meta.get("format", ""),
            views, likes, comments, shares,
            round(analytics["avg_view_duration"], 1), round(analytics["avg_view_pct"], 2),
            analytics["subs_gained"], meta.get("upload_hour_utc", ""), publish_date,
            early_retention,
        ])

    # Long-form parity loop (2026-07-31) - best-effort, never breaks the
    # Shorts sync above even if it fails entirely.
    try:
        sync_longform_videos(token, video_meta, today)
    except Exception as e:  # noqa: BLE001 - long-form sync must never break Shorts sync
        print(f"[analytics] long-form sync failed unexpectedly (non-fatal): {e}")

    # Channel-level "returning viewers" signal - once per run, best-effort.
    snapshot = get_channel_audience_snapshot(token)
    if snapshot:
        print(f"[analytics] channel audience snapshot: subscribed_views={snapshot['subscribed']} "
              f"unsubscribed_views={snapshot['unsubscribed']} subscribed_share_pct={snapshot['share_pct']}")
        append_channel_audience_snapshot(token, snapshot)
    else:
        print("[analytics] channel audience snapshot unavailable this run (non-fatal)")

    print("[analytics] done")


if __name__ == "__main__":
    main()
