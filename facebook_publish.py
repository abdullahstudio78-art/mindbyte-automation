"""
MindByte Automation - Facebook Page cross-posting via the Graph API Reels
publishing flow.

Self-contained (no other pipeline module imports it at module load time),
matching the tiktok_publish.py pattern - pipeline.py calls
`post_short_to_facebook()` as a best-effort step right after the YouTube
upload, and ANY failure here (missing secrets, network error, API error,
etc.) must never abort the main pipeline run. The video has already been
safely published to YouTube by the time this runs.

Note (2026-08-19): unlike the TikTok cross-post, pipeline.py does NOT pass
this the same output_path used for YouTube. Facebook has no "subscribe"
concept, so the YouTube cut's spoken "don't forget to subscribe" CTA and
burned-in "SUBSCRIBE" end card would read oddly there. pipeline.py builds
a separate video first (same story clips/voiceover, but a re-recorded
"like, share, follow"-style closing line and a matching end card) and
passes that file's path here instead - this module just uploads whatever
video_path it's given.

--------------------------------------------------------------------------
Token setup (2026-08-19, done via the Meta for Developers dashboard +
Graph API Explorer)
--------------------------------------------------------------------------
FACEBOOK_PAGE_ACCESS_TOKEN is a long-lived PAGE token obtained by:
  1. Creating a Meta developer app ("MindByte Automation", App ID
     993766050361265) with the Pages API use case, granted
     pages_manage_posts / pages_read_engagement / pages_show_list /
     business_management for the "MindByte" page (Page ID 1185853201284632).
  2. Generating a short-lived User token in Graph API Explorer, extending it
     to a 60-day long-lived User token via the Access Token Debugger's
     "Extend Access Token" button.
  3. Calling GET /me/accounts?fields=id,name,access_token with that
     long-lived User token - the Page token returned there does NOT expire
     on its own as long as the underlying login session isn't revoked
     (confirmed via the Access Token Debugger: Expires = "Never").

Required GitHub Actions secrets:
  FACEBOOK_PAGE_ACCESS_TOKEN  - the long-lived, non-expiring Page token above
  FACEBOOK_PAGE_ID            - 1185853201284632 ("MindByte")
Optional (not used for posting yet, kept for future token-refresh logic):
  FACEBOOK_APP_ID             - 993766050361265
  FACEBOOK_APP_SECRET
"""

import os
import re
import time

import requests

FACEBOOK_PAGE_ACCESS_TOKEN = os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN", "")
FACEBOOK_PAGE_ID = os.environ.get("FACEBOOK_PAGE_ID", "")

GRAPH_VERSION = "v26.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"


def facebook_configured() -> bool:
    """False when the Page token/ID aren't set yet - lets pipeline.py skip
    Facebook posting cleanly instead of failing on missing secrets."""
    return bool(FACEBOOK_PAGE_ACCESS_TOKEN and FACEBOOK_PAGE_ID)


MAX_HASHTAGS = 8
FACEBOOK_CAPTION_LIMIT = 2200  # Meta's stated Reels description limit


def _tag_to_hashtag(tag: str) -> str | None:
    """'relationship psychology' -> '#RelationshipPsychology'. Drops
    anything that doesn't reduce to at least one real word, or that would
    make an unreasonably long/short hashtag."""
    words = re.findall(r"[A-Za-z0-9]+", tag)
    if not words:
        return None
    hashtag = "#" + "".join(w.capitalize() for w in words)
    if len(hashtag) < 4 or len(hashtag) > 30:
        return None
    return hashtag


def _build_hashtags(tags: list) -> list:
    seen = set()
    hashtags = []
    for tag in tags or []:
        hashtag = _tag_to_hashtag(str(tag))
        if hashtag and hashtag.lower() not in seen:
            seen.add(hashtag.lower())
            hashtags.append(hashtag)
        if len(hashtags) >= MAX_HASHTAGS:
            break
    return hashtags


def build_facebook_caption(title: str, description: str, tags: list) -> str:
    """Full SEO-optimized Facebook caption: the hook title, the same
    topic description the SEO pass wrote for YouTube (stripped of any
    YouTube-only stock-footage/music attribution lines, which don't apply
    here and would just read as clutter), a set of hashtags derived from
    the same search-term tags YouTube uses, and a native like/share/follow
    line (the burned-in CTA in the video itself covers the same ground,
    this just backs it up in text so it also surfaces in Facebook search
    and the feed caption)."""
    title = (title or "").strip()
    description = (description or "").strip()

    # Strip attribution lines pipeline.py appends to its LOCAL description
    # copy before this is called elsewhere (e.g. "Music: ... by ...",
    # credit lines) - if a caller passes the raw script description this
    # is a no-op, but it's cheap insurance either way.
    clean_lines = [
        line for line in description.splitlines()
        if not re.match(r"^\s*(Music:|Video by|Photo by)", line, re.IGNORECASE)
    ]
    description = "\n".join(clean_lines).strip()
    # Collapse the blank-line gaps left behind by stripped attribution lines.
    description = re.sub(r"\n{3,}", "\n\n", description)

    hashtags = _build_hashtags(tags)

    parts = []
    if title:
        parts.append(title)
    if description and description != title:
        parts.append(description)
    if hashtags:
        parts.append(" ".join(hashtags))
    parts.append("\U0001F44D Like this video, share it with someone who needs it, and follow MindByte for more.")

    caption = "\n\n".join(parts)
    return caption[:FACEBOOK_CAPTION_LIMIT]


def _start_upload_session() -> dict:
    resp = requests.post(
        f"{GRAPH_BASE}/{FACEBOOK_PAGE_ID}/video_reels",
        data={
            "upload_phase": "start",
            "access_token": FACEBOOK_PAGE_ACCESS_TOKEN,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "video_id" not in data or "upload_url" not in data:
        raise RuntimeError(f"Facebook Reels upload start failed: {data}")
    return data


def _upload_video_bytes(upload_url: str, video_path: str) -> None:
    video_size = os.path.getsize(video_path)
    with open(video_path, "rb") as f:
        video_bytes = f.read()
    resp = requests.post(
        upload_url,
        headers={
            "Authorization": f"OAuth {FACEBOOK_PAGE_ACCESS_TOKEN}",
            "offset": "0",
            "file_size": str(video_size),
        },
        data=video_bytes,
        timeout=180,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Facebook video upload failed ({resp.status_code}): {resp.text[:500]}")
    data = resp.json()
    if not data.get("success", False):
        raise RuntimeError(f"Facebook video upload did not report success: {data}")


def _finish_and_publish(video_id: str, description: str) -> dict:
    resp = requests.post(
        f"{GRAPH_BASE}/{FACEBOOK_PAGE_ID}/video_reels",
        data={
            "upload_phase": "finish",
            "video_id": video_id,
            "video_state": "PUBLISHED",
            "description": description[:2200],
            "access_token": FACEBOOK_PAGE_ACCESS_TOKEN,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success", False):
        raise RuntimeError(f"Facebook Reels finish/publish failed: {data}")
    return data


def _poll_publish_status(video_id: str, max_wait_s: int = 120) -> str:
    """Reels processing is async - poll status_type until it leaves the
    processing state, capped at max_wait_s (best-effort, non-fatal on
    timeout - the video can still finish publishing after this returns)."""
    waited = 0
    while waited < max_wait_s:
        resp = requests.get(
            f"{GRAPH_BASE}/{video_id}",
            params={"fields": "status", "access_token": FACEBOOK_PAGE_ACCESS_TOKEN},
            timeout=30,
        )
        if resp.status_code == 200:
            status = resp.json().get("status", {}).get("video_status", "unknown")
            if status in ("ready", "published"):
                return status
            if status == "error":
                return "error"
        time.sleep(5)
        waited += 5
    return "timed_out_waiting"


def post_short_to_facebook(video_path: str, title: str, description: str, tags: list = None) -> dict:
    """Best-effort Facebook Reels upload for a finished Shorts video.

    Returns a dict {"posted": bool, "video_id": str|None, "status": str,
    "reason": str|None} - never raises, so pipeline.py can call this
    unconditionally without a try/except of its own. The video must still
    exist on disk when this is called - call it before the temp dir (and
    its final.mp4) is cleaned up, same as the TikTok cross-post.

    `description` should be the SAME topic-SEO description the script
    generation step wrote (script["description"]) - not the YouTube
    description with attribution lines already appended - and `tags`
    should be script["tags"], the same search-term list YouTube uses.
    build_facebook_caption() turns those into a full title + description +
    hashtags + CTA caption (2026-08-19, "fully SEO optimized caption with
    good topic description" per user request - previously this posted
    with only the bare title).
    """
    if not facebook_configured():
        return {"posted": False, "video_id": None, "status": "skipped",
                 "reason": "Facebook secrets not configured yet"}

    try:
        caption = build_facebook_caption(title, description, tags or [])

        session = _start_upload_session()
        video_id = session["video_id"]
        upload_url = session["upload_url"]

        _upload_video_bytes(upload_url, video_path)
        _finish_and_publish(video_id, caption)
        status = _poll_publish_status(video_id)

        return {"posted": status in ("ready", "published"), "video_id": video_id,
                 "status": status, "reason": None}
    except Exception as e:  # noqa: BLE001 - must never abort the pipeline run
        return {"posted": False, "video_id": None, "status": "error", "reason": str(e)}
