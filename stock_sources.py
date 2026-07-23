"""
stock_sources.py - multi-source Category A stock footage aggregator.

Previously pipeline.py only ever pulled B-roll from Pexels. Per the user's
explicit direction (2026-07-23 design discussion), the pipeline should not
depend on a single stock source - it should try several YouTube-safe,
commercially-licensed libraries and fall back across them.

Sources included, and why:
    - Pexels  - existing integration, official API, clear commercial license.
    - Pixabay - official API (pixabay.com/api/docs), video search included,
      commercial/monetized YouTube use permitted, no attribution required.
    - Coverr  - official API (api.coverr.co/docs), commercial license,
      attribution required on the free tier (handled by the caller adding
      a description credit line, same pattern already used for CC-BY music
      in pipeline.py's fetch_background_music()).

Sources deliberately EXCLUDED from automation, after checking each site's
actual terms:
    - Mixkit - their Terms of Service explicitly forbid automated/bulk
      scraping ("use scripts or bots to mass download Items... this
      includes using any means whatsoever to scrape/download the entire
      library"). Automating against Mixkit would violate their ToS outright.
    - Videvo - no public developer API exists, and license type (royalty-free
      vs. attribution-required vs. editorial-only) varies per individual clip
      with no way to query it programmatically. Not automatable safely.

Every search_*_clip() function here returns the SAME normalized shape as the
existing pipeline.search_pexels_clip():
    {"id": <str>, "url": <str>, "source": <str>, "attribution": <str|None>}
or None if nothing usable was found - so gather_clips() can try sources in
order and fall through exactly like the existing FALLBACK_QUERIES rotation,
without needing to know which source actually served a given clip.

API keys are read as optional env vars (PIXABAY_API_KEY, COVERR_API_KEY).
Missing keys quietly disable that source (return None immediately) rather
than raising, so a repo/CI environment that hasn't added the new secrets yet
keeps working exactly as before, on Pexels alone.
"""

import os
import requests

SESSION = requests.Session()

PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY")
COVERR_API_KEY = os.environ.get("COVERR_API_KEY")


def search_pixabay_clip(query: str, used_ids: set) -> dict | None:
    """Search Pixabay Videos. Returns a normalized clip dict or None.

    Pixabay's video API doesn't support a portrait/orientation filter the
    way Pexels does - most Pixabay video results are landscape. That's fine
    here: assemble_video() already scale+crops every clip (regardless of
    source) to the target 1080x1920 frame, so a landscape source clip is
    handled the same way a landscape Pexels fallback clip already is.
    """
    if not PIXABAY_API_KEY:
        return None
    try:
        resp = SESSION.get(
            "https://pixabay.com/api/videos/",
            params={"key": PIXABAY_API_KEY, "q": query, "per_page": 15},
            timeout=30,
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    for hit in resp.json().get("hits", []):
        vid_id = hit.get("id")
        if vid_id is None or vid_id in used_ids:
            continue
        videos = hit.get("videos", {})
        # Prefer the largest rendition available; Pixabay's keys are
        # "large" -> "medium" -> "small" -> "tiny", largest first.
        for size in ("large", "medium", "small", "tiny"):
            file_info = videos.get(size)
            if file_info and file_info.get("url"):
                used_ids.add(vid_id)
                return {
                    "id": f"pixabay:{vid_id}", "url": file_info["url"],
                    "source": "pixabay", "attribution": None,
                }
    return None


def search_coverr_clip(query: str, used_ids: set) -> dict | None:
    """Search Coverr. Returns a normalized clip dict or None.

    Coverr's free-tier license requires attribution (to the clip's creator
    or to Coverr.co) - the caller is responsible for adding a credit line
    to the video description when attribution is not None, same pattern
    pipeline.py already uses for non-CC0 background music credits.
    """
    if not COVERR_API_KEY:
        return None
    try:
        resp = SESSION.get(
            "https://api.coverr.co/videos",
            params={"query": query, "urls": "true", "page_size": 15, "api_key": COVERR_API_KEY},
            timeout=30,
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    for hit in resp.json().get("hits", []):
        vid_id = hit.get("id")
        if vid_id is None or vid_id in used_ids:
            continue
        url = (hit.get("urls") or {}).get("mp4")
        if url:
            used_ids.add(vid_id)
            return {
                "id": f"coverr:{vid_id}", "url": url, "source": "coverr",
                "attribution": "Video by Coverr.co",
            }
    return None


def search_multi_source_clip(query: str, used_ids: set, pexels_search_fn) -> dict | None:
    """Try Pexels, then Pixabay, then Coverr, in that order, for one query.

    `pexels_search_fn` is pipeline.search_pexels_clip, passed in rather than
    imported directly, since pipeline.py imports THIS module - importing
    pipeline.py back here would create a circular import. Pexels stays
    first because it's the most-proven/highest-hit-rate source in this
    pipeline's history; Pixabay and Coverr are genuinely-licensed
    additional attempts, not replacements, per the "don't depend on a
    single source" direction.
    """
    clip = pexels_search_fn(query, used_ids)
    if clip:
        clip.setdefault("source", "pexels")
        clip.setdefault("attribution", None)
        return clip
    clip = search_pixabay_clip(query, used_ids)
    if clip:
        return clip
    return search_coverr_clip(query, used_ids)
