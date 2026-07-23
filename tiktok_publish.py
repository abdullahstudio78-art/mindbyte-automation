"""
MindByte Automation - TikTok publishing via the official Content Posting API.

This module is deliberately self-contained (no other pipeline module imports
it at module load time failing the whole run) - pipeline.py calls
`post_short_to_tiktok()` as a best-effort step right after the YouTube
upload, and ANY failure here (missing secrets, network error, API error,
audit not yet approved, etc.) must never abort the main pipeline run. The
video has already been safely published to YouTube by the time this runs.

--------------------------------------------------------------------------
IMPORTANT - privacy_level and the audit
--------------------------------------------------------------------------
Until TikTok approves this app's Content Posting API audit, ALL posts from
unaudited apps are forced server-side to SELF_ONLY (private, visible only to
the account owner) regardless of what privacy_level is requested - this is
enforced by TikTok, not something this code can bypass. TIKTOK_PRIVACY_LEVEL
defaults to "SELF_ONLY" for that reason. Once the audit is approved, change
the TIKTOK_PRIVACY_LEVEL GitHub secret to "PUBLIC_TO_EVERYONE" (or
"MUTUAL_FOLLOW_FRIENDS" / "FOLLOWER_OF_CREATOR" if preferred) and posts will
start going out publicly with no code change needed.

--------------------------------------------------------------------------
One-time setup (see tiktok_auth.py for the interactive OAuth step)
--------------------------------------------------------------------------
Required GitHub Actions secrets:
  TIKTOK_CLIENT_KEY     - from the TikTok developer app dashboard
  TIKTOK_CLIENT_SECRET  - from the TikTok developer app dashboard
  TIKTOK_REFRESH_TOKEN  - produced once by running tiktok_auth.py locally
Optional:
  TIKTOK_PRIVACY_LEVEL  - defaults to "SELF_ONLY" (see note above)
"""

import os
import time

import requests

TIKTOK_CLIENT_KEY = os.environ.get("TIKTOK_CLIENT_KEY", "")
TIKTOK_CLIENT_SECRET = os.environ.get("TIKTOK_CLIENT_SECRET", "")
TIKTOK_REFRESH_TOKEN = os.environ.get("TIKTOK_REFRESH_TOKEN", "")
TIKTOK_PRIVACY_LEVEL = os.environ.get("TIKTOK_PRIVACY_LEVEL", "SELF_ONLY")

TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"

CHUNK_SIZE = 10 * 1024 * 1024  # 10MB - TikTok's recommended chunk size


def tiktok_configured() -> bool:
    """False when the developer app hasn't been set up yet - lets
    pipeline.py skip TikTok posting cleanly (with a clear log line) instead
    of failing on missing secrets while the audit/setup is still pending."""
    return bool(TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET and TIKTOK_REFRESH_TOKEN)


def get_tiktok_access_token() -> str:
    resp = requests.post(
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"},
        data={
            "client_key": TIKTOK_CLIENT_KEY,
            "client_secret": TIKTOK_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": TIKTOK_REFRESH_TOKEN,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"TikTok token refresh failed: {data}")
    return data["access_token"]


def _init_upload(access_token: str, title: str, video_size: int) -> dict:
    chunk_count = max(1, (video_size + CHUNK_SIZE - 1) // CHUNK_SIZE)
    resp = requests.post(
        INIT_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        json={
            "post_info": {
                "title": title[:150],
                "privacy_level": TIKTOK_PRIVACY_LEVEL,
                "disable_duplicate_check": True,
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": video_size,
                "chunk_size": min(CHUNK_SIZE, video_size),
                "total_chunk_count": chunk_count,
            },
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("error", {}).get("code") not in (None, "ok"):
        raise RuntimeError(f"TikTok publish init failed: {data}")
    return data["data"]


def _upload_video_bytes(upload_url: str, video_path: str, video_size: int) -> None:
    with open(video_path, "rb") as f:
        video_bytes = f.read()
    resp = requests.put(
        upload_url,
        headers={
            "Content-Type": "video/mp4",
            "Content-Range": f"bytes 0-{video_size - 1}/{video_size}",
        },
        data=video_bytes,
        timeout=120,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"TikTok video upload failed ({resp.status_code}): {resp.text[:500]}")


def _poll_publish_status(access_token: str, publish_id: str, max_wait_s: int = 120) -> str:
    waited = 0
    while waited < max_wait_s:
        resp = requests.post(
            STATUS_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json={"publish_id": publish_id},
            timeout=30,
        )
        resp.raise_for_status()
        status = resp.json().get("data", {}).get("status", "UNKNOWN")
        if status in ("PUBLISH_COMPLETE", "FAILED"):
            return status
        time.sleep(5)
        waited += 5
    return "TIMED_OUT_WAITING"


def post_short_to_tiktok(video_path: str, title: str, description: str) -> dict:
    """Best-effort TikTok upload for a finished Shorts video.

    Returns a dict {"posted": bool, "publish_id": str|None, "status": str,
    "reason": str|None} - never raises, so pipeline.py can call this
    unconditionally without a try/except of its own. NOTE: the video must
    still exist on disk when this is called - pipeline.py calls it from
    inside the same `with tempfile.TemporaryDirectory()` block as the
    YouTube upload, before the temp dir (and its final.mp4) is cleaned up.
    """
    if not tiktok_configured():
        return {"posted": False, "publish_id": None, "status": "skipped",
                 "reason": "TikTok secrets not configured yet - app registration/audit pending"}

    try:
        access_token = get_tiktok_access_token()
        video_size = os.path.getsize(video_path)
        init_data = _init_upload(access_token, title, video_size)
        publish_id = init_data["publish_id"]
        upload_url = init_data["upload_url"]

        _upload_video_bytes(upload_url, video_path, video_size)
        status = _poll_publish_status(access_token, publish_id)

        return {"posted": status == "PUBLISH_COMPLETE", "publish_id": publish_id,
                 "status": status, "reason": None}
    except Exception as e:  # noqa: BLE001 - must never abort the pipeline run
        return {"posted": False, "publish_id": None, "status": "error", "reason": str(e)}
