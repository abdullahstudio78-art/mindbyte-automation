"""One-off (re-runnable) backfill: add every already-published back-catalog
video to its pillar+format playlist. The 2026-09-03 playlist feature
(pipeline.py's assign_video_to_playlist()) only fires for videos published
AFTER it shipped - every video published before that date has no playlist at
all. This script closes that gap once, using the exact same playlist
registry / get-or-create / skip-trend-sourced logic as the live pipeline, so
back-catalog videos land in the same playlists new uploads do.

Safe to run more than once: before adding a video, it checks the target
playlist's actual current contents (not just a local log) and skips videos
already in it, so a second run is a no-op rather than a duplicate-add.

Reads:
  - Videos!A2:D and LongformVideos!A2:D for (video_id, status) - Shorts and
    long-form, same "Scheduled"/"Published" filter weekly_review.py uses.
  - VideoMeta!A2:V for (pillar, format, trend_source) per video_id, same
    columns weekly_review.py's load_video_meta() reads.

Skips (same design as the live pipeline, see pipeline.py's
assign_video_to_playlist() docstring):
  - videos with no pillar on record (can't pick a playlist without one)
  - trend-sourced videos (trend_source non-blank) - trend content never
    shares a playlist with evergreen content
"""

import pipeline as p


def load_video_ids(access_token: str, tab: str) -> list:
    try:
        rows = p.sheet_get(access_token, f"{tab}!A2:D")
    except Exception as e:  # noqa: BLE001
        print(f"[backfill] could not read {tab} (skipping): {e}")
        return []
    out = []
    for row in rows:
        row = row + [""] * (4 - len(row))
        video_id, status = row[0].strip(), row[3].strip()
        if video_id and status in ("Scheduled", "Published"):
            out.append(video_id)
    return out


def load_meta(access_token: str) -> dict:
    rows = p.sheet_get(access_token, "VideoMeta!A2:V")
    meta = {}
    for row in rows:
        row = row + [""] * (22 - len(row))
        video_id = row[0].strip()
        if not video_id:
            continue
        meta[video_id] = {
            "pillar": row[3].strip(),
            "format": row[4].strip() or "short",
            "trend_source": row[20].strip(),
        }
    return meta


def existing_playlist_video_ids(access_token: str, playlist_id: str) -> set:
    """Fetches every video ID already in a playlist, paginated, so the
    backfill never double-adds on a re-run."""
    ids = set()
    page_token = None
    while True:
        params = {"part": "contentDetails", "playlistId": playlist_id, "maxResults": 50}
        if page_token:
            params["pageToken"] = page_token
        resp = p.SESSION.get(
            "https://www.googleapis.com/youtube/v3/playlistItems",
            params=params, headers=p.google_headers(access_token), timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("items", []):
            vid = item.get("contentDetails", {}).get("videoId")
            if vid:
                ids.add(vid)
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return ids


def run() -> None:
    token = p.get_access_token()
    video_ids = set(load_video_ids(token, "Videos")) | set(load_video_ids(token, "LongformVideos"))
    print(f"[backfill] {len(video_ids)} published/scheduled videos on record")

    meta = load_meta(token)
    playlist_cache: dict = {}  # (pillar, fmt) -> (playlist_id, set of video ids already in it)

    added, skipped_no_pillar, skipped_trend, skipped_already_in, failed = 0, 0, 0, 0, 0

    for video_id in sorted(video_ids):
        m = meta.get(video_id)
        if not m or not m["pillar"]:
            skipped_no_pillar += 1
            continue
        if m["trend_source"]:
            skipped_trend += 1
            continue
        pillar, fmt = m["pillar"], m["format"]
        key = (pillar, fmt)
        if key not in playlist_cache:
            try:
                playlist_id = p.get_or_create_playlist(token, pillar, fmt)
            except Exception as e:  # noqa: BLE001
                print(f"[backfill] could not resolve playlist for {key}, skipping its videos: {e}")
                playlist_cache[key] = (None, set())
                playlist_id = None
            if playlist_id is not None:
                # Listing a playlist's contents can 404 for a brief window
                # right after it's freshly created (API propagation delay) -
                # that's not a real failure, it just means the playlist has
                # no videos in it yet. Treat any listing failure as "assume
                # empty" rather than abandoning the whole pillar/format
                # bucket, since the playlist itself was created successfully.
                try:
                    existing = existing_playlist_video_ids(token, playlist_id)
                except Exception as e:  # noqa: BLE001
                    print(f"[backfill] could not list existing contents of playlist {playlist_id} "
                          f"(assuming empty - likely just-created): {e}")
                    existing = set()
                playlist_cache[key] = (playlist_id, existing)
        playlist_id, existing_ids = playlist_cache[key]
        if playlist_id is None:
            failed += 1
            continue
        if video_id in existing_ids:
            skipped_already_in += 1
            continue
        try:
            p.add_video_to_playlist(token, playlist_id, video_id)
            existing_ids.add(video_id)
            added += 1
            print(f"[backfill] added {video_id} -> '{p._playlist_title_for(pillar, fmt)}'")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"[backfill] FAILED to add {video_id} to '{p._playlist_title_for(pillar, fmt)}': {e}")

    print(
        f"[backfill] done: added={added}, already_in_playlist={skipped_already_in}, "
        f"skipped_no_pillar={skipped_no_pillar}, skipped_trend_sourced={skipped_trend}, failed={failed}"
    )


if __name__ == "__main__":
    run()
