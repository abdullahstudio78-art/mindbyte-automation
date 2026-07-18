"""
MindByte Automation - daily analytics sync.

Pulls current views/likes/comments/shares for every video logged in the
Videos sheet and writes the latest numbers back, so the channel owner never
needs to open YouTube Studio.
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


def get_video_shares(token: str, video_id: str) -> int:
    """Shares aren't in the Data API's basic statistics - pull from
    YouTube Analytics API instead. Falls back to 0 if unavailable (e.g.
    the video is too new for analytics data to have landed yet)."""
    resp = SESSION.get(
        "https://youtubeanalytics.googleapis.com/v2/reports",
        headers=headers(token),
        params={
            "ids": f"channel=={YOUTUBE_CHANNEL_ID}",
            "startDate": "2020-01-01",
            "endDate": datetime.now(timezone.utc).date().isoformat(),
            "metrics": "shares",
            "filters": f"video=={video_id}",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        return 0
    rows = resp.json().get("rows", [])
    if not rows:
        return 0
    return int(rows[0][0])


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


def main() -> None:
    token = get_access_token()
    rows = sheet_get(token, "Videos!A2:O")
    print(f"[analytics] {len(rows)} rows in Videos sheet")

    for i, row in enumerate(rows, start=2):  # sheet row 2 is the first data row
        video_id = row[0].strip() if row else ""
        if not video_id:
            continue
        stats = get_video_stats(token, video_id)
        if not stats:
            print(f"[analytics] row {i}: video {video_id} not found (may still be private)")
            continue
        shares = get_video_shares(token, video_id)
        views = stats.get("viewCount", 0)
        likes = stats.get("likeCount", 0)
        comments = stats.get("commentCount", 0)
        now = datetime.now(timezone.utc).isoformat()

        # Columns I-M are Views, Likes, Comments, Shares, Last Synced.
        sheet_update(token, f"Videos!I{i}:M{i}", [views, likes, comments, shares, now])
        print(f"[analytics] row {i}: video {video_id} -> views={views} likes={likes} "
              f"comments={comments} shares={shares}")

    print("[analytics] done")


if __name__ == "__main__":
    main()
