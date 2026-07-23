"""
MindByte Automation - weekly content review.

Runs once a week (Sundays, via .github/workflows/weekly_review.yml) after
analytics_sync.py has had all week to keep view/like/comment/share counts
current in the "Videos" sheet. This script:

  1. Reads every logged video row (published or not) from the Videos sheet.
  2. Ranks published videos by engagement (likes+comments per view) and by
     raw views, to find this week's best- and worst-performing topics.
  3. Asks Groq for a short, concrete production plan for the coming week -
     topics/angles to lean into, ones to drop, based on what actually
     performed - and writes it to a new "WeeklyPlan" tab so the channel
     owner (and next week's pipeline runs) can see it at a glance.

Known limitation (flagged, not silently ignored): the Videos sheet doesn't
yet store per-video pillar/emotion/footage-category tags (that tracking is
part of the still-to-be-built Cinematic Footage Library metadata store -
see MINDBYTE_CINEMATIC_PIPELINE_V2_SPEC.md section 4/9). Until that exists,
this script correlates on Topic/Title text only. Once section 4's metadata
store is live, this script should be updated to join on those richer tags
instead of raw text.
"""

import os
from datetime import datetime, timedelta, timezone

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
    "WeekOf", "TopPerformers", "WeakPerformers", "PlanNotes", "GeneratedAt",
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


def sheet_get(token: str, a1_range: str) -> list:
    resp = SESSION.get(f"{SHEETS_BASE}/values/{a1_range}", headers=headers(token), timeout=30)
    resp.raise_for_status()
    return resp.json().get("values", [])


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


def call_groq(prompt: str) -> str:
    resp = SESSION.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": GROQ_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.4,
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def safe_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def main() -> None:
    token = get_access_token()
    rows = sheet_get(token, "Videos!A2:O")
    print(f"[weekly] {len(rows)} total logged rows in Videos sheet")

    # Only videos actually published (have a real VideoID and view count)
    # are meaningful for performance ranking - rejected/failed rows are
    # skipped here (they're still visible for debugging in the Videos tab
    # itself, just not part of the performance correlation).
    published = []
    for row in rows:
        row = row + [""] * (15 - len(row))  # pad short rows
        video_id, title, topic, status = row[0], row[1], row[2], row[3]
        views, likes, comments, shares = safe_int(row[8]), safe_int(row[9]), safe_int(row[10]), safe_int(row[11])
        if not video_id or status not in ("Scheduled", "Published"):
            continue
        engagement_rate = (likes + comments) / views if views > 0 else 0.0
        published.append({
            "video_id": video_id, "title": title, "topic": topic,
            "views": views, "likes": likes, "comments": comments,
            "shares": shares, "engagement_rate": engagement_rate,
        })

    if not published:
        print("[weekly] no published videos with analytics data yet - skipping plan generation")
        return

    by_views = sorted(published, key=lambda v: v["views"], reverse=True)
    by_engagement = sorted(published, key=lambda v: v["engagement_rate"], reverse=True)

    top_n = by_views[:5]
    bottom_n = by_views[-5:] if len(by_views) > 5 else []

    top_summary = "\n".join(
        f"- \"{v['title']}\" (topic: {v['topic']}) - {v['views']} views, "
        f"engagement rate {v['engagement_rate']:.3f}"
        for v in top_n
    )
    bottom_summary = "\n".join(
        f"- \"{v['title']}\" (topic: {v['topic']}) - {v['views']} views, "
        f"engagement rate {v['engagement_rate']:.3f}"
        for v in bottom_n
    ) or "(not enough published videos yet to identify weak performers)"

    prompt = f"""You are the content strategist for MindByte, a cinematic psychology YouTube channel
(footage-driven, no custom characters - documentary-style narration over real B-roll).
Priority order: 1. Storytelling 2. Viewer retention 3. Emotional connection 4. Visual quality
5. Cinematic identity 6. Automation 7. Scale.

This week's top-performing videos (by views):
{top_summary}

This week's weakest-performing videos (by views):
{bottom_summary}

Write a short, concrete production plan for next week in plain text (no markdown headers, no bullet
symbols - short paragraphs or a simple numbered list only):
1. What emotional themes / topic angles to lean into next week, based on what worked.
2. What to drop or change about the weak performers (be specific: was it likely the hook, the topic
   itself, pacing, or thumbnail?).
3. 3 concrete new topic ideas for next week that build on what's working.
Keep it under 200 words."""

    plan_notes = call_groq(prompt)
    print("[weekly] plan generated:\n" + plan_notes)

    week_of = datetime.now(timezone.utc).date().isoformat()
    row = [
        week_of,
        " | ".join(f"{v['title']} ({v['views']}v)" for v in top_n),
        " | ".join(f"{v['title']} ({v['views']}v)" for v in bottom_n) if bottom_n else "",
        plan_notes,
        datetime.now(timezone.utc).isoformat(),
    ]

    try:
        sheet_append(token, "WeeklyPlan!A:E", row)
    except Exception as e:  # noqa: BLE001 - logging must never crash the run
        print(f"[weekly] WeeklyPlan tab append failed ({e}), attempting to create it")
        if ensure_sheet_tab(token, "WeeklyPlan", WEEKLY_PLAN_HEADER):
            sheet_append(token, "WeeklyPlan!A:E", row)
        else:
            print("[weekly] could not create WeeklyPlan tab - plan printed above only")

    print("[weekly] done")


if __name__ == "__main__":
    main()
