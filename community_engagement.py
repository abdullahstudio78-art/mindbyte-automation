"""
MindByte Automation - Community Engagement Pipeline (2026-08-01).

Automated discovery + AI-drafted comments on relevant psychology videos,
built to grow MindByteFacts' reputation in-niche - WITHOUT ever posting
anything you haven't personally reviewed. See docs/community-engagement.md
for the full design writeup and the YouTube policy research behind it.

Why this is split into three modes instead of one linear script:

YouTube's API Developer Policies require "the user's prior specific and
express consent" before the API automates a comment, with "final
authority" resting on the channel owner for each action. A single
autonomous find-draft-post loop would violate that. So this script never
posts anything in the same run it drafts it - there is always a human
review step (you, approving rows in the CommentQueue sheet tab) between
drafting and publishing.

Modes (select via `python community_engagement.py <mode>`):

  generate   Discover candidate videos across the 6 niche topics, draft a
             ~month's worth of comments (BATCH_DAYS x DAILY_COMMENT_LIMIT),
             run every safety/quality check, and write PASSING drafts to
             the CommentQueue tab as status=pending_review, Approved=blank.
             Writes NOTHING to YouTube. Safe to run as often as you like -
             it dedupes against videos already seen.

  publish    Reads CommentQueue rows where Approved=TRUE and Status is
             still pending_review, takes up to DAILY_COMMENT_LIMIT of them
             (oldest ScheduledDate first), posts each via
             commentThreads.insert with a random delay between posts, and
             writes the result (posted/failed + CommentID) back to the
             row. Never touches a row that hasn't been explicitly
             approved. Respects COMMENTING_ENABLED as a hard kill switch.

  learn      Best-effort: for comments already posted, re-checks like/reply
             counts via comments.list and appends a snapshot to
             CommunityEngagementResults, so weekly_review.py's pattern
             detection can eventually fold "which topics/videos produced
             engagement" into its report. Never required for the other two
             modes to work; degrades to a no-op on any failure.

Every mode is wrapped so a single bad video/API response never crashes
the whole run - it logs and continues, matching the rest of this
codebase's degrade-gracefully philosophy.
"""

import difflib
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from brand_rules import CHANNEL_IDENTITY_LINE, CHANNEL_NICHE_LIST, GENERIC_PHRASES
import community_engagement_config as cfg

import os

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
OAUTH_CLIENT_ID = os.environ["OAUTH_CLIENT_ID"]
OAUTH_CLIENT_SECRET = os.environ["OAUTH_CLIENT_SECRET"]
OAUTH_REFRESH_TOKEN = os.environ["OAUTH_REFRESH_TOKEN"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
YOUTUBE_CHANNEL_ID = os.environ.get("YOUTUBE_CHANNEL_ID", "")

# 2026-08-18: llama-3.3-70b-versatile was decommissioned by Groq (404
# model_not_found). Switched to the current production model + fallback.
GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_MODEL_FALLBACKS = ["openai/gpt-oss-20b"]
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
SHEETS_BASE = f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_SHEET_ID}"
SESSION = requests.Session()


# ---------------------------------------------------------------------------
# Auth / Sheets helpers (identical pattern to analytics_sync.py / weekly_review.py)
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
            print(f"[community] Sheets call got {resp.status_code} - retrying in {wait_s:.1f}s")
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
        return []
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


def sheet_update_row(token: str, tab_name: str, row_number: int, row: list) -> None:
    """1-indexed sheet row number (i.e. row 2 = first data row after header)."""
    a1_range = f"{tab_name}!A{row_number}:Z{row_number}"
    resp = _sheets_call_with_retry(
        lambda: SESSION.put(
            f"{SHEETS_BASE}/values/{a1_range}",
            headers=headers(token),
            params={"valueInputOption": "USER_ENTERED"},
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


def append_with_selfheal(token: str, tab_name: str, header: list, row: list) -> None:
    a1_range = f"{tab_name}!A:Z"
    try:
        sheet_append(token, a1_range, row)
    except Exception as e:  # noqa: BLE001
        print(f"[community] {tab_name} append failed ({e}), attempting to create it")
        if ensure_sheet_tab(token, tab_name, header):
            try:
                sheet_append(token, a1_range, row)
            except Exception as e2:
                print(f"[community] still could not append to {tab_name} after creating it: {e2}")
        else:
            print(f"[community] could not create {tab_name} tab - row not logged")


def log_action(token: str, action: str, video_id: str, query: str, details: str) -> None:
    row = [datetime.now(timezone.utc).isoformat(), action, video_id, query, details]
    append_with_selfheal(token, cfg.ENGAGEMENT_LOG_TAB, cfg.ENGAGEMENT_LOG_HEADER, row)
    print(f"[community] {action}: video={video_id} query={query} :: {details}")


def call_groq(prompt: str, _retries: int = 2) -> str:
    models_to_try = [GROQ_MODEL] + list(GROQ_MODEL_FALLBACKS)
    for model in models_to_try:
        for attempt in range(_retries + 1):
            resp = SESSION.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.7,
                    # 2026-08-18: see pipeline.py's call_groq for why this is
                    # needed - openai/gpt-oss-* burns part of the completion
                    # budget on hidden reasoning, and Groq's default
                    # max_completion_tokens (1024) left too little room for
                    # even a short comment once reasoning is subtracted.
                    "reasoning_effort": "low",
                    "max_completion_tokens": 2048,
                },
                timeout=60,
            )
            if resp.status_code == 429 and attempt < _retries:
                wait_s = 5.0
                match = re.search(r"try again in ([\d.]+)s", resp.text)
                if match:
                    wait_s = float(match.group(1)) + 1.0
                print(f"[community] Groq rate-limited - waiting {wait_s:.1f}s and retrying")
                time.sleep(wait_s)
                continue
            if resp.status_code == 404 and model != models_to_try[-1]:
                print(f"[community] Groq model '{model}' unavailable (404) - falling back")
                break
            try:
                resp.raise_for_status()
            except requests.exceptions.HTTPError:
                break
            return resp.json()["choices"][0]["message"]["content"].strip()
    return ""


# ---------------------------------------------------------------------------
# YouTube discovery (same endpoints/pattern as external_trends.py)
# ---------------------------------------------------------------------------

def search_videos(token: str, query: str) -> list:
    published_after = (
        datetime.now(timezone.utc) - timedelta(days=cfg.DISCOVERY_LOOKBACK_DAYS)
    ).isoformat().replace("+00:00", "Z")
    try:
        search_resp = SESSION.get(
            "https://www.googleapis.com/youtube/v3/search",
            headers=headers(token),
            params={
                "part": "snippet",
                "type": "video",
                "q": query,
                "order": "relevance",
                "publishedAfter": published_after,
                "maxResults": cfg.MAX_RESULTS_PER_QUERY,
                "relevanceLanguage": "en",
            },
            timeout=30,
        )
        if search_resp.status_code != 200:
            print(f"[community] search failed for '{query}': {search_resp.status_code} {search_resp.text[:200]}")
            return []
        items = search_resp.json().get("items", [])
        video_ids = [it["id"]["videoId"] for it in items if it.get("id", {}).get("videoId")]
        if not video_ids:
            return []

        videos_resp = SESSION.get(
            "https://www.googleapis.com/youtube/v3/videos",
            headers=headers(token),
            params={"part": "snippet,statistics", "id": ",".join(video_ids)},
            timeout=30,
        )
        if videos_resp.status_code != 200:
            print(f"[community] videos.list failed for '{query}': {videos_resp.status_code}")
            return []

        channel_ids = set()
        video_items = videos_resp.json().get("items", [])
        for it in video_items:
            cid = it.get("snippet", {}).get("channelId")
            if cid:
                channel_ids.add(cid)

        sub_counts = {}
        if channel_ids:
            ch_resp = SESSION.get(
                "https://www.googleapis.com/youtube/v3/channels",
                headers=headers(token),
                params={"part": "statistics", "id": ",".join(channel_ids)},
                timeout=30,
            )
            if ch_resp.status_code == 200:
                for it in ch_resp.json().get("items", []):
                    sub_counts[it["id"]] = int(it.get("statistics", {}).get("subscriberCount", 0) or 0)

        results = []
        for it in video_items:
            snippet = it.get("snippet", {})
            channel_id = snippet.get("channelId", "")
            results.append({
                "video_id": it.get("id", ""),
                "title": snippet.get("title", ""),
                "description": snippet.get("description", ""),
                "channel_id": channel_id,
                "channel_title": snippet.get("channelTitle", ""),
                "subscriber_count": sub_counts.get(channel_id, 0),
                "query": query,
            })
        return results
    except Exception as e:  # noqa: BLE001
        print(f"[community] search_videos exception for '{query}': {e}")
        return []


def passes_channel_filters(video: dict) -> bool:
    if cfg.EXCLUDE_OWN_CHANNEL and YOUTUBE_CHANNEL_ID and video["channel_id"] == YOUTUBE_CHANNEL_ID:
        return False
    subs = video.get("subscriber_count", 0)
    if cfg.MIN_CHANNEL_SUBSCRIBERS is not None and subs < cfg.MIN_CHANNEL_SUBSCRIBERS:
        return False
    if cfg.MAX_CHANNEL_SUBSCRIBERS is not None and subs > cfg.MAX_CHANNEL_SUBSCRIBERS:
        return False
    return True


# ---------------------------------------------------------------------------
# Comment drafting
# ---------------------------------------------------------------------------

def draft_comment(video: dict) -> str:
    prompt = f"""You are writing a single YouTube comment as a knowledgeable, warm
human viewer who happens to know a lot about psychology - NOT as a brand,
NOT as a bot, NOT as {CHANNEL_IDENTITY_LINE}'s official account.

Video title: {video['title']}
Video description (may be partial/promotional, use only for context): {video['description'][:500]}
Channel: {video['channel_title']}

Write ONE short comment (2-4 sentences, under 350 characters) that:
- Reacts specifically to something in THIS video's title/topic - not generic.
- Adds one real, correct psychology insight, example, or reframe that builds on
  the video's point (teaches the reader something extra, doesn't just praise it).
- Ends with an open, genuine question or thought that invites replies - real
  curiosity, not "what do you guys think?" filler.
- Sounds like a real person typed it: contractions, natural rhythm, no
  hashtags, no emojis, no exclamation-point spam.

STRICTLY FORBIDDEN, your comment will be discarded if it contains any of:
- Any link or URL of any kind.
- Any mention of another channel, subscribing, following, or "check out".
- Generic praise with no substance ("great video!", "so true", "love this").
- Any phrase from this ban list: {", ".join(GENERIC_PHRASES)}

Respond with ONLY the comment text, nothing else - no quotes, no preamble."""
    try:
        text = call_groq(prompt)
        return text.strip().strip('"').strip()
    except Exception as e:  # noqa: BLE001
        print(f"[community] draft_comment failed for {video['video_id']}: {e}")
        return ""


# ---------------------------------------------------------------------------
# Safety checks - every one of these must pass before a draft is queued
# ---------------------------------------------------------------------------

def relevance_check(comment: str, video: dict) -> float:
    """Very deliberately a simple keyword-overlap heuristic, not an NLP
    model - conservative on purpose (reject on doubt rather than trust an
    LLM's own self-assessment of its relevance). Returns the overlap count
    used as a score; caller compares against MIN_RELEVANCE_KEYWORD_OVERLAP."""
    stopwords = {
        "the", "a", "an", "is", "are", "was", "were", "this", "that", "and",
        "or", "but", "of", "to", "in", "on", "for", "with", "you", "your",
        "it", "its", "how", "why", "what", "not", "do", "does", "did",
    }
    title_words = {w.lower() for w in re.findall(r"[a-zA-Z']+", video["title"]) if w.lower() not in stopwords and len(w) > 3}
    comment_words = {w.lower() for w in re.findall(r"[a-zA-Z']+", comment) if w.lower() not in stopwords}
    overlap = title_words & comment_words
    return len(overlap)


def contains_banned_pattern(comment: str) -> str:
    """Returns the matched banned phrase, or '' if clean."""
    lower = comment.lower()
    for phrase in cfg.BANNED_COMMENT_PATTERNS + GENERIC_PHRASES:
        if phrase.lower() in lower:
            return phrase
    return ""


def contains_link(comment: str) -> bool:
    return bool(re.search(r"https?://|www\.|\.(com|net|org|io|co)\b", comment, re.IGNORECASE))


def similarity_against_history(comment: str, past_comments: list) -> float:
    best = 0.0
    for past in past_comments:
        score = difflib.SequenceMatcher(None, comment.lower(), past.lower()).ratio()
        best = max(best, score)
    return best


def load_recent_comment_history(token: str) -> list:
    """Pulls DraftComment values from CommentQueue for similarity checking -
    both queued and already-posted, within SIMILARITY_LOOKBACK_DAYS."""
    rows = sheet_get(token, f"{cfg.COMMENT_QUEUE_TAB}!A2:Z")
    cutoff = datetime.now(timezone.utc) - timedelta(days=cfg.SIMILARITY_LOOKBACK_DAYS)
    header = cfg.COMMENT_QUEUE_HEADER
    idx = {name: i for i, name in enumerate(header)}
    out = []
    for row in rows:
        try:
            created_at = row[idx["CreatedAt"]] if len(row) > idx["CreatedAt"] else ""
            if created_at:
                created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                if created < cutoff:
                    continue
            draft = row[idx["DraftComment"]] if len(row) > idx["DraftComment"] else ""
            if draft:
                out.append(draft)
        except Exception:
            continue
    return out


def load_seen_video_ids(token: str) -> set:
    rows = sheet_get(token, f"{cfg.SEEN_VIDEOS_TAB}!A2:A")
    return {row[0] for row in rows if row}


def run_all_safety_checks(comment: str, video: dict, history: list) -> tuple:
    """Returns (passed: bool, reason: str, relevance_score: int, similarity_score: float)."""
    if not comment:
        return False, "empty draft", 0, 0.0
    if not (cfg.MIN_COMMENT_CHARS <= len(comment) <= cfg.MAX_COMMENT_CHARS):
        return False, f"length {len(comment)} out of bounds", 0, 0.0
    if cfg.FORBID_LINKS_IN_COMMENTS and contains_link(comment):
        return False, "contains a link", 0, 0.0
    banned = contains_banned_pattern(comment)
    if banned:
        return False, f"banned phrase: '{banned}'", 0, 0.0
    relevance = relevance_check(comment, video)
    if relevance < cfg.MIN_RELEVANCE_KEYWORD_OVERLAP:
        return False, f"low relevance overlap ({relevance})", relevance, 0.0
    similarity = similarity_against_history(comment, history)
    if similarity >= cfg.SIMILARITY_THRESHOLD:
        return False, f"too similar to a recent comment ({similarity:.2f})", relevance, similarity
    return True, "ok", relevance, similarity


# ---------------------------------------------------------------------------
# Mode: generate
# ---------------------------------------------------------------------------

def mode_generate() -> None:
    token = get_access_token()
    seen_ids = load_seen_video_ids(token)
    history = load_recent_comment_history(token)
    batch_month = datetime.now(timezone.utc).strftime("%Y-%m")
    target = cfg.BATCH_TARGET_SIZE

    candidates = []
    for query in cfg.DISCOVERY_QUERIES:
        videos = search_videos(token, query)
        for v in videos:
            if v["video_id"] in seen_ids:
                continue
            if not passes_channel_filters(v):
                continue
            candidates.append(v)
        time.sleep(1)  # gentle pacing against quota, matches external_trends.py's spirit

    random.shuffle(candidates)
    print(f"[community] {len(candidates)} candidate videos after filtering, targeting {target} drafts")

    accepted = 0
    rejected = 0
    for video in candidates:
        if accepted >= target:
            break

        seen_ids.add(video["video_id"])
        append_with_selfheal(
            token, cfg.SEEN_VIDEOS_TAB, cfg.SEEN_VIDEOS_HEADER,
            [video["video_id"], video["channel_title"], video["title"], video["query"],
             datetime.now(timezone.utc).isoformat()],
        )

        comment = draft_comment(video)
        passed, reason, relevance, similarity = run_all_safety_checks(comment, video, history)

        if not passed:
            rejected += 1
            log_action(token, "draft_rejected", video["video_id"], video["query"], reason)
            continue

        history.append(comment)  # so later drafts in this same run also get compared against it
        scheduled_day = accepted // cfg.DAILY_COMMENT_LIMIT
        scheduled_date = (datetime.now(timezone.utc) + timedelta(days=scheduled_day)).strftime("%Y-%m-%d")
        video_url = f"https://www.youtube.com/watch?v={video['video_id']}"

        row = [
            batch_month, video["video_id"], video_url, video["title"], video["channel_title"],
            video["query"], comment, str(relevance), f"{similarity:.2f}",
            "pending_review", "", scheduled_date, "", "", "",
            datetime.now(timezone.utc).isoformat(),
        ]
        append_with_selfheal(token, cfg.COMMENT_QUEUE_TAB, cfg.COMMENT_QUEUE_HEADER, row)
        log_action(token, "draft_queued", video["video_id"], video["query"], f"scheduled {scheduled_date}")
        accepted += 1

    print(f"[community] generate done: {accepted} drafts queued, {rejected} rejected by safety checks")
    print(f"[community] Review the '{cfg.COMMENT_QUEUE_TAB}' tab and set Approved=TRUE on rows you want published.")


# ---------------------------------------------------------------------------
# Mode: publish
# ---------------------------------------------------------------------------

def post_comment(token: str, video_id: str, comment: str) -> dict:
    resp = SESSION.post(
        "https://www.googleapis.com/youtube/v3/commentThreads",
        headers=headers(token),
        params={"part": "snippet"},
        json={
            "snippet": {
                "videoId": video_id,
                "topLevelComment": {"snippet": {"textOriginal": comment}},
            }
        },
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"commentThreads.insert failed: {resp.status_code} {resp.text[:300]}")
    return resp.json()


def mode_publish() -> None:
    if not cfg.COMMENTING_ENABLED:
        print("[community] COMMENTING_ENABLED is False in community_engagement_config.py - skipping publish entirely.")
        return

    token = get_access_token()
    rows = sheet_get(token, f"{cfg.COMMENT_QUEUE_TAB}!A2:Z")
    header = cfg.COMMENT_QUEUE_HEADER
    idx = {name: i for i, name in enumerate(header)}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    eligible = []  # (sheet_row_number, row_values)
    for i, row in enumerate(rows):
        row_number = i + 2  # header is row 1
        padded = row + [""] * (len(header) - len(row))
        status = padded[idx["Status"]]
        approved = padded[idx["Approved"]].strip().upper()
        scheduled = padded[idx["ScheduledDate"]]
        if status == "pending_review" and approved == "TRUE" and scheduled <= today:
            eligible.append((row_number, padded))

    eligible.sort(key=lambda pair: pair[1][idx["ScheduledDate"]])
    to_post = eligible[: cfg.DAILY_COMMENT_LIMIT]
    print(f"[community] {len(eligible)} approved+due rows, posting up to {cfg.DAILY_COMMENT_LIMIT}")

    for n, (row_number, row) in enumerate(to_post):
        video_id = row[idx["VideoID"]]
        comment = row[idx["DraftComment"]]
        try:
            result = post_comment(token, video_id, comment)
            comment_id = result.get("id", "")
            comment_url = f"https://www.youtube.com/watch?v={video_id}&lc={comment_id}"
            row[idx["Status"]] = "posted"
            row[idx["PostedAt"]] = datetime.now(timezone.utc).isoformat()
            row[idx["CommentID"]] = comment_id
            row[idx["CommentURL"]] = comment_url
            sheet_update_row(token, cfg.COMMENT_QUEUE_TAB, row_number, row)
            log_action(token, "posted", video_id, row[idx["Query"]], comment_id)
        except Exception as e:  # noqa: BLE001
            row[idx["Status"]] = "failed"
            sheet_update_row(token, cfg.COMMENT_QUEUE_TAB, row_number, row)
            log_action(token, "post_failed", video_id, row[idx["Query"]], str(e))

        if n < len(to_post) - 1:
            delay = random.uniform(cfg.MIN_DELAY_SECONDS, cfg.MAX_DELAY_SECONDS)
            print(f"[community] sleeping {delay:.0f}s before next post")
            time.sleep(delay)

    if not eligible:
        pending_count = sum(
            1 for row in rows
            if len(row) > idx["Status"] and row[idx["Status"]] == "pending_review"
        )
        if pending_count == 0:
            print("[community] CommentQueue is empty - run mode=generate to draft a new batch.")
        else:
            print(f"[community] {pending_count} drafts still awaiting your review/approval in the sheet.")


# ---------------------------------------------------------------------------
# Mode: learn (feeds weekly_review.py's pattern detection)
# ---------------------------------------------------------------------------

def mode_learn() -> None:
    token = get_access_token()
    rows = sheet_get(token, f"{cfg.COMMENT_QUEUE_TAB}!A2:Z")
    header = cfg.COMMENT_QUEUE_HEADER
    idx = {name: i for i, name in enumerate(header)}

    posted = [
        row for row in rows
        if len(row) > idx["Status"] and row[idx["Status"]] == "posted" and row[idx["CommentID"]]
    ]
    print(f"[community] checking engagement on {len(posted)} posted comments")

    for row in posted:
        comment_id = row[idx["CommentID"]] if len(row) > idx["CommentID"] else ""
        video_id = row[idx["VideoID"]] if len(row) > idx["VideoID"] else ""
        query = row[idx["Query"]] if len(row) > idx["Query"] else ""
        if not comment_id:
            continue
        try:
            resp = SESSION.get(
                "https://www.googleapis.com/youtube/v3/comments",
                headers=headers(token),
                params={"part": "snippet", "id": comment_id},
                timeout=30,
            )
            if resp.status_code != 200:
                continue
            items = resp.json().get("items", [])
            if not items:
                continue
            snippet = items[0].get("snippet", {})
            like_count = snippet.get("likeCount", 0)
            reply_count = 0  # top-level comments.list doesn't return reply count directly; left for a future commentThreads.list pass
            log_row = [
                datetime.now(timezone.utc).isoformat(), comment_id, video_id,
                str(like_count), str(reply_count), query,
            ]
            append_with_selfheal(token, cfg.ENGAGEMENT_RESULTS_TAB, cfg.ENGAGEMENT_RESULTS_HEADER, log_row)
        except Exception as e:  # noqa: BLE001
            print(f"[community] learn: skipping {comment_id} ({e})")
            continue

    print("[community] learn done - see CommunityEngagementResults tab. "
          "weekly_review.py can be extended to fold this into its pattern report.")


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ("generate", "publish", "learn"):
        print("Usage: python community_engagement.py [generate|publish|learn]")
        sys.exit(1)
    mode = sys.argv[1]
    if mode == "generate":
        mode_generate()
    elif mode == "publish":
        mode_publish()
    elif mode == "learn":
        mode_learn()


if __name__ == "__main__":
    main()
