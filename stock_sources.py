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

Every search_*_candidates() function here returns a LIST of candidates in
the SAME normalized shape as before:
    {"id": <str>, "url": <str>, "source": <str>, "attribution": <str|None>}
(empty list if nothing usable was found) - so gather_clips() can try
candidates in order, download+inspect each with no_human_filter, and move
to the next candidate/source/fallback query the moment one is rejected,
instead of committing to whatever the first search hit happened to be.

IMPORTANT (2026-07-23): these functions do NOT mutate `used_ids` themselves
anymore - they only READ it, to skip ids already spent elsewhere in the
video. The CALLER (gather_clips) is responsible for adding an id to
`used_ids` only once it actually downloads and accepts that candidate
(passes the no-human check). This changed because the old
"return the first unused hit" design meant a rejected (person-containing)
candidate could never be reconsidered or skipped in favor of the next
result - there was no "next result," only ever one.

API keys are read as optional env vars (PIXABAY_API_KEY, COVERR_API_KEY).
Missing keys quietly disable that source (return [] immediately) rather
than raising, so a repo/CI environment that hasn't added the new secrets yet
keeps working exactly as before, on Pexels alone.
"""

import os
import requests

SESSION = requests.Session()

PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY")
COVERR_API_KEY = os.environ.get("COVERR_API_KEY")

# How many candidates to consider per source per query before giving up on
# that query - each candidate may need to be downloaded and inspected by
# no_human_filter before being accepted or rejected, so this is a real
# cost/thoroughness tradeoff, not just an API page size.
MAX_CANDIDATES_PER_SOURCE = 6


def search_pixabay_candidates(query: str, used_ids: set, limit: int = MAX_CANDIDATES_PER_SOURCE) -> list:
    """Search Pixabay Videos. Returns up to `limit` normalized candidate
    dicts (empty list if none/failed).

    Pixabay's video API doesn't support a portrait/orientation filter the
    way Pexels does - most Pixabay video results are landscape. That's fine
    here: assemble_video() already scale+crops every clip (regardless of
    source) to the target 1080x1920 frame, so a landscape source clip is
    handled the same way a landscape Pexels fallback clip already is.
    """
    if not PIXABAY_API_KEY:
        return []
    try:
        resp = SESSION.get(
            "https://pixabay.com/api/videos/",
            params={"key": PIXABAY_API_KEY, "q": query, "per_page": 15},
            timeout=30,
        )
    except requests.RequestException:
        return []
    if resp.status_code != 200:
        return []
    candidates = []
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
                candidates.append({
                    "id": f"pixabay:{vid_id}", "url": file_info["url"],
                    "source": "pixabay", "attribution": None,
                })
                break
        if len(candidates) >= limit:
            break
    return candidates


def search_coverr_candidates(query: str, used_ids: set, limit: int = MAX_CANDIDATES_PER_SOURCE) -> list:
    """Search Coverr. Returns up to `limit` normalized candidate dicts
    (empty list if none/failed).

    Coverr's free-tier license requires attribution (to the clip's creator
    or to Coverr.co) - the caller is responsible for adding a credit line
    to the video description when attribution is not None, same pattern
    pipeline.py already uses for non-CC0 background music credits.
    """
    if not COVERR_API_KEY:
        return []
    try:
        resp = SESSION.get(
            "https://api.coverr.co/videos",
            params={"query": query, "urls": "true", "page_size": 15, "api_key": COVERR_API_KEY},
            timeout=30,
        )
    except requests.RequestException:
        return []
    if resp.status_code != 200:
        return []
    candidates = []
    for hit in resp.json().get("hits", []):
        vid_id = hit.get("id")
        if vid_id is None or vid_id in used_ids:
            continue
        url = (hit.get("urls") or {}).get("mp4")
        if url:
            candidates.append({
                "id": f"coverr:{vid_id}", "url": url, "source": "coverr",
                "attribution": "Video by Coverr.co",
            })
        if len(candidates) >= limit:
            break
    return candidates


def search_multi_source_candidates(query: str, used_ids: set, pexels_candidates_fn) -> list:
    """Try Pexels, then Pixabay, then Coverr, in that order, for one query -
    returns a single combined list of candidates (Pexels' first, in each
    source's own relevance order), for the caller to download+inspect one
    at a time until one passes the no-human check.

    `pexels_candidates_fn` is pipeline.search_pexels_candidates, passed in
    rather than imported directly, since pipeline.py imports THIS module -
    importing pipeline.py back here would create a circular import. Pexels
    stays first because it's the most-proven/highest-hit-rate source in
    this pipeline's history; Pixabay and Coverr are genuinely-licensed
    additional attempts, not replacements, per the "don't depend on a
    single source" direction.
    """
    candidates = []
    for c in pexels_candidates_fn(query, used_ids):
        c.setdefault("source", "pexels")
        c.setdefault("attribution", None)
        candidates.append(c)
    candidates.extend(search_pixabay_candidates(query, used_ids))
    candidates.extend(search_coverr_candidates(query, used_ids))
    return candidates
