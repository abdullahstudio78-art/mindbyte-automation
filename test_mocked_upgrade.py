"""
Mocked-call smoke test for the 2026-07-31 upgrade. No real network/API
access - every HTTP call is mocked. Run with: python test_mocked_upgrade.py
"""
import json
import os
import sys
import types
from datetime import datetime, timezone
from unittest import mock

import requests

os.environ.setdefault("OAUTH_CLIENT_ID", "x")
os.environ.setdefault("OAUTH_CLIENT_SECRET", "x")
os.environ.setdefault("OAUTH_REFRESH_TOKEN", "x")
os.environ.setdefault("GOOGLE_SHEET_ID", "x")
os.environ.setdefault("YOUTUBE_CHANNEL_ID", "x")
os.environ.setdefault("GROQ_API_KEY", "x")
os.environ.setdefault("PEXELS_API_KEY", "x")

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")


def fake_resp(status=200, json_data=None):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = json_data or {}
    r.raise_for_status = mock.Mock()
    if status >= 400:
        r.raise_for_status.side_effect = Exception(f"HTTP {status}")
    return r


# --- 1. analytics_sync.py long-form loop with a fake video -----------------
import analytics_sync as asy

with mock.patch.object(asy.SESSION, "get") as mget, mock.patch.object(asy.SESSION, "put") as mput, \
     mock.patch.object(asy.SESSION, "post") as mpost:

    def get_side_effect(url, **kwargs):
        if "LongformVideos" in url:
            return fake_resp(200, {"values": [["lf123", "Test LF Video", "topic", "Published", "2026-07-01"]]})
        if "youtube/v3/videos" in url:
            return fake_resp(200, {"items": [{"statistics": {"viewCount": "500", "likeCount": "20", "commentCount": "3"}}]})
        if "youtubeanalytics" in url and "elapsedVideoTimeRatio" in kwargs.get("params", {}).get("dimensions", ""):
            return fake_resp(200, {"rows": [["0.02", "0.9"], ["0.06", "0.7"]]})
        if "youtubeanalytics" in url and "subscribedStatus" in kwargs.get("params", {}).get("dimensions", ""):
            return fake_resp(200, {"rows": [["SUBSCRIBED", "100"], ["UNSUBSCRIBED", "400"]]})
        if "youtubeanalytics" in url:
            return fake_resp(200, {"rows": [["3", "45.0", "60.0", "1"]]})
        return fake_resp(200, {})

    mget.side_effect = get_side_effect
    mput.return_value = fake_resp(200, {})
    mpost.return_value = fake_resp(200, {})

    try:
        token = "fake-token"
        video_meta = {}
        asy.sync_longform_videos(token, video_meta, "2026-07-31")
        check("analytics_sync.sync_longform_videos runs without raising", True)
    except Exception as e:
        check(f"analytics_sync.sync_longform_videos runs without raising ({e})", False)

    try:
        ret = asy.get_early_retention_pct("fake-token", "lf123", is_longform=True)
        check("analytics_sync.get_early_retention_pct returns a float", isinstance(ret, float))
    except Exception as e:
        check(f"analytics_sync.get_early_retention_pct ({e})", False)

    try:
        snap = asy.get_channel_audience_snapshot("fake-token")
        check("analytics_sync.get_channel_audience_snapshot returns dict with share_pct=20.0",
              snap is not None and snap["share_pct"] == 20.0)
    except Exception as e:
        check(f"analytics_sync.get_channel_audience_snapshot ({e})", False)


# --- 2. external_trends.py full run with mocked search/videos --------------
import external_trends as et

with mock.patch.object(et.SESSION, "post") as mpost2, mock.patch.object(et.SESSION, "get") as mget2:
    def post_side_effect(url, **kwargs):
        if "oauth2.googleapis.com/token" in url:
            return fake_resp(200, {"access_token": "fake-token"})
        return fake_resp(200, {})

    def get_side_effect2(url, **kwargs):
        if "youtube/v3/search" in url:
            return fake_resp(200, {"items": [{"id": {"videoId": "abc123"}}]})
        if "youtube/v3/videos" in url:
            return fake_resp(200, {"items": [{
                "snippet": {"title": "Why You Overthink Everything?", "channelTitle": "OtherChannel",
                            "publishedAt": "2026-07-20T00:00:00Z"},
                "statistics": {"viewCount": "10000"},
                "contentDetails": {"duration": "PT3M30S"},
            }]})
        return fake_resp(200, {})

    mpost2.side_effect = post_side_effect
    mget2.side_effect = get_side_effect2

    try:
        et.main()
        check("external_trends.main() completes without raising", True)
    except Exception as e:
        check(f"external_trends.main() completes without raising ({e})", False)

# Failure-path: everything errors -> must still exit cleanly
with mock.patch.object(et.SESSION, "post", side_effect=Exception("network down")):
    try:
        et.main()
        check("external_trends.main() degrades cleanly on total failure", True)
    except Exception as e:
        check(f"external_trends.main() degrades cleanly on total failure ({e})", False)


# --- 3. weekly_review.py confidence-scoring with synthetic pattern groups --
import weekly_review as wr

high_n_groups = {
    "A": {"score": 0.9, "n": 10},
    "B": {"score": 0.2, "n": 10},
}
check("confidence_for_group: high-n + big separation -> High",
      wr.confidence_for_group(high_n_groups, "A", "B") == "High")

low_n_groups = {
    "A": {"score": 0.9, "n": 1},
    "B": {"score": 0.2, "n": 1},
}
check("confidence_for_group: n below MIN_GROUP_SAMPLE -> Low",
      wr.confidence_for_group(low_n_groups, "A", "B") == "Low")

medium_n_groups = {
    "A": {"score": 0.6, "n": 3},
    "B": {"score": 0.5, "n": 3},
}
check("confidence_for_group: moderate n, small separation -> Medium",
      wr.confidence_for_group(medium_n_groups, "A", "B") == "Medium")

check("confidence_for_group: no best key -> Low",
      wr.confidence_for_group({}, None, None) == "Low")


# --- 3b. weekly_review.py main() must not crash on a Groq outage -----------
# Regression test for the 2026-07-31 fix: call_groq() at the main() call site
# used to be unwrapped, so a non-429 Groq error (5xx, network failure, etc.)
# would raise uncaught and crash the whole weekly run. It should now be
# caught and degrade to "skip this week's report," matching every other
# external call in this file.
with mock.patch.object(wr, "get_access_token", return_value="fake-token"), \
     mock.patch.object(wr, "load_videos", return_value=[{"video_id": "v1", "views": 10}]), \
     mock.patch.object(wr, "load_longform_videos", return_value=[]), \
     mock.patch.object(wr, "load_analytics_history_latest", return_value={}), \
     mock.patch.object(wr, "load_video_meta", return_value={}), \
     mock.patch.object(wr, "load_competitor_trends", return_value=[]), \
     mock.patch.object(wr, "merge_records", return_value=[{"video_id": "v1", "composite_score": 0.5}]), \
     mock.patch.object(wr, "compute_composite_scores", return_value=None), \
     mock.patch.object(wr, "detect_patterns", return_value=({}, False)), \
     mock.patch.object(wr, "build_groq_prompt", return_value="fake prompt"), \
     mock.patch.object(wr, "call_groq", side_effect=Exception("Groq 503 - service unavailable")):
    try:
        wr.main()
        check("weekly_review.main() degrades cleanly on a Groq outage (no crash)", True)
    except Exception as e:
        check(f"weekly_review.main() degrades cleanly on a Groq outage ({e})", False)


# --- 4. pipeline.py log_video_meta() backward compatibility ----------------
import pipeline as pl

with mock.patch.object(pl, "sheet_append") as msa:
    msa.return_value = None
    try:
        # Old call style - only the original positional args, no new kwargs
        pl.log_video_meta(
            "fake-token", "vid1", "Title", "topic", "pillar",
            "short", "hook text", "structure", 150, 17, 55.0, ["tag1", "tag2"],
        )
        check("pipeline.log_video_meta old call style (no new kwargs) still works", msa.called)
    except Exception as e:
        check(f"pipeline.log_video_meta old call style still works ({e})", False)

with mock.patch.object(pl, "sheet_append") as msa2:
    msa2.return_value = None
    try:
        pl.log_video_meta(
            "fake-token", "vid2", "Title", "topic", "pillar",
            "short", "hook text", "structure", 150, 17, 55.0, ["tag1"],
            hook_type="question", series="Weird Minds", thumbnail_identity="thumb.png",
        )
        args, kwargs = msa2.call_args
        row = args[-1]
        check("pipeline.log_video_meta new kwargs land in the appended row (hook_type/series/thumb)",
              row[-5] == "question" and row[-4] == "Weird Minds" and row[-3] == "thumb.png")
    except Exception as e:
        check(f"pipeline.log_video_meta new kwargs land correctly ({e})", False)

with mock.patch.object(pl, "sheet_append") as msa3:
    msa3.return_value = None
    try:
        pl.log_video_meta(
            "fake-token", "vid3", "Title", "topic", "pillar",
            "short", "hook text", "structure", 150, 17, 55.0, ["tag1"],
            cta_style="curiosity", cta_text="Subscribe for the next one.",
        )
        args, kwargs = msa3.call_args
        row = args[-1]
        check("pipeline.log_video_meta CTA kwargs land in the appended row (cta_style/cta_text)",
              row[-2] == "curiosity" and row[-1] == "Subscribe for the next one.")
    except Exception as e:
        check(f"pipeline.log_video_meta CTA kwargs land correctly ({e})", False)


# --- Groq model-fallback (2026-08-18 outage fix) ------------------------
# llama-3.3-70b-versatile was decommissioned by Groq (404 model_not_found),
# which silently broke every Publish Video / Publish Long-Form Video /
# Weekly Content Review run for ~2 weeks. Verifies call_groq() now falls
# back to the next configured model on a 404 instead of crashing the whole
# pipeline the same way again.
class _FakeGroqResp:
    def __init__(self, status, payload):
        self.status_code = status
        self.text = json.dumps(payload)
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error", response=self)


for mod, label in ((pl, "pipeline"), (wr, "weekly_review")):
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None, _calls=calls):
        _calls.append(json["model"])
        if json["model"] == mod.GROQ_MODEL:
            return _FakeGroqResp(404, {"error": {"message": "does not exist", "code": "model_not_found"}})
        return _FakeGroqResp(200, {"choices": [{"message": {"content": "OK from fallback"}}]})

    with mock.patch.object(mod.SESSION, "post", side_effect=fake_post):
        try:
            result = mod.call_groq("test prompt")
            check(f"{label}.call_groq falls back to next model on 404 model_not_found",
                  result == "OK from fallback" and calls == [mod.GROQ_MODEL] + list(mod.GROQ_MODEL_FALLBACKS))
        except Exception as e:
            check(f"{label}.call_groq falls back on 404 ({e})", False)


# --- Winning Content Profile (2026-08-18 historical intelligence) -------
def _mk_record(topic, hook, score, days_ago):
    from datetime import timedelta
    return {
        "topic": topic, "hook_opener": hook, "pillar": "Social Psychology",
        "structure": "hook-explain-reveal", "word_count": 120, "length_sec": 45,
        "upload_hour": "14", "tags": [], "subs_gained": 0, "views": 1000,
        "composite_score": score, "recency_weight": 1.0,
        "publish_at": (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(),
        "cta_style": "curiosity",
    }


try:
    records = (
        [_mk_record("Winning Topic", "curiosity_question", 0.9, i) for i in range(9)]
        + [_mk_record("Weak Topic", "generic_intro", 0.1, i) for i in range(3)]
    )
    patterns, _ = wr.detect_patterns(records)
    profile = wr.build_winning_content_profile(records, patterns)
    check("weekly_review.build_winning_content_profile classifies a strong, well-sampled "
          "topic as winning",
          any(t["key"] == "Winning Topic" for t in profile["winning_topics"]))
    check("weekly_review.build_winning_content_profile classifies a below-baseline topic as weak",
          any(t["key"] == "Weak Topic" for t in profile["weak_topics"]))
    check("weekly_review.build_winning_content_profile sets last_updated", bool(profile.get("last_updated")))
except Exception as e:
    check(f"build_winning_content_profile classification ({e})", False)

try:
    fatigue_records = [_mk_record("Repeaty Topic", "curiosity_question", 0.5, i) for i in range(10)]
    warnings_found = wr.detect_fatigue(fatigue_records)
    check("weekly_review.detect_fatigue flags a topic repeated in all recent videos",
          any("Repeaty Topic" in w for w in warnings_found))
    check("weekly_review.detect_fatigue flags a hook repeated in all recent videos",
          any("curiosity_question" in w for w in warnings_found))
except Exception as e:
    check(f"detect_fatigue ({e})", False)

try:
    empty_profile = wr.build_winning_content_profile([], {})
    check("weekly_review.build_winning_content_profile handles an empty dataset without raising",
          empty_profile == {})
except Exception as e:
    check(f"build_winning_content_profile empty dataset ({e})", False)

with mock.patch.object(pl, "sheet_get") as msg:
    msg.return_value = [["2026-08-17", json.dumps({"weak_topics": [{"key": "Weak Topic", "score": 0.1, "n": 3}]}), "x"]]
    try:
        weak = pl.load_weak_topics("fake-token")
        check("pipeline.load_weak_topics parses the latest WinningContentProfile row",
              weak == {"weak topic"})
    except Exception as e:
        check(f"pipeline.load_weak_topics parsing ({e})", False)

with mock.patch.object(pl, "sheet_get") as msg_fail:
    msg_fail.side_effect = RuntimeError("Sheets down")
    try:
        weak = pl.load_weak_topics("fake-token")
        check("pipeline.load_weak_topics degrades cleanly (empty set) on a Sheets failure", weak == set())
    except Exception as e:
        check(f"pipeline.load_weak_topics degrades on failure ({e})", False)


# --- summary -----------------------------------------------------------
print("\n=== SUMMARY ===")
n_pass = sum(1 for _, ok in results if ok)
for name, ok in results:
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
print(f"\n{n_pass}/{len(results)} checks passed")
sys.exit(0 if n_pass == len(results) else 1)
