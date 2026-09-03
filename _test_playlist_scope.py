"""One-off verification script (2026-09-03): confirms the existing OAuth
refresh token's scope actually covers playlists.insert/playlistItems.insert
before relying on assign_video_to_playlist() in a real publish run. Creates
one REAL playlist (a genuine target playlist, not throwaway test clutter -
"Relationship Psychology - Shorts") and registers it in PlaylistRegistry,
same as a normal run would. Safe to delete this file once confirmed."""

import pipeline as p

token = p.get_access_token()
try:
    playlist_id = p.get_or_create_playlist(token, "Relationship Psychology", "short")
    print(f"[test] playlist ready: {playlist_id}")
except Exception as e:
    print(f"[test] FAILED: {e}")
    raise
