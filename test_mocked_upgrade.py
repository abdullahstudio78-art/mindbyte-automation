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


# --- Sheets/YouTube API rate-limit backoff (2026-08-19) ------------------
# Groq calls already retried on 429; Sheets and the per-video YouTube Data/
# Analytics calls had zero protection - a transient rate limit either
# crashed a publish run (Sheets) or silently degraded real data to zeros
# (YouTube Analytics), per the growth-system audit's explicitly flagged
# gap. Verifies the new _api_call_with_retry() helpers actually retry
# instead of giving up on the first 429/5xx.
import pipeline as pl
import community_engagement as ce

def _fake_resp(status, payload):
    r = mock.Mock(status_code=status)
    r.json.return_value = payload
    if status >= 400:
        r.raise_for_status.side_effect = requests.exceptions.HTTPError(f"{status} error", response=r)
    else:
        r.raise_for_status.return_value = None
    return r


with mock.patch.object(pl.SESSION, "get") as mget, mock.patch("time.sleep"):
    mget.side_effect = [_fake_resp(429, {}), _fake_resp(200, {"values": [["a", "b"]]})]
    try:
        rows = pl.sheet_get("fake-token", "Videos!A2:B")
        check("pipeline.sheet_get retries once on a 429 and succeeds on the next attempt",
              rows == [["a", "b"]] and mget.call_count == 2)
    except Exception as e:
        check(f"pipeline.sheet_get 429 retry ({e})", False)

with mock.patch.object(pl.SESSION, "get") as mget_exhaust, mock.patch("time.sleep"):
    mget_exhaust.return_value = _fake_resp(500, {})
    try:
        pl.sheet_get("fake-token", "Videos!A2:B")
        check("pipeline.sheet_get raises after exhausting retries on persistent 500s", False)
    except requests.exceptions.HTTPError:
        check("pipeline.sheet_get raises after exhausting retries on persistent 500s", True)
    except Exception as e:
        check(f"pipeline.sheet_get persistent-500 behavior ({e})", False)

with mock.patch.object(asy.SESSION, "get") as mget_yt, mock.patch("time.sleep"):
    mget_yt.side_effect = [_fake_resp(429, {}), _fake_resp(200, {"rows": [[5, 42.0, 71.5, 2]]})]
    try:
        result = asy.get_video_analytics("fake-token", "vid1")
        check("analytics_sync.get_video_analytics retries on 429 instead of silently returning zeros",
              result["shares"] == 5 and mget_yt.call_count == 2)
    except Exception as e:
        check(f"analytics_sync.get_video_analytics 429 retry ({e})", False)


# --- 4. pipeline.py log_video_meta() backward compatibility ----------------

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


# --- Permanent fix: live model discovery when ALL configured models 404 ---
# (2026-08-18) GROQ_MODEL_FALLBACKS is itself a fixed list, so it can go
# stale exactly the way GROQ_MODEL did. Verifies that when every configured
# model 404s, call_groq() queries Groq's /models endpoint and retries with
# whatever it finds live, instead of requiring another manual code patch
# the next time a model is decommissioned.
for mod, label in ((pl, "pipeline"), (wr, "weekly_review"), (ce, "community_engagement")):
    calls = []

    def fake_post_all_dead(url, headers=None, json=None, timeout=None, _calls=calls):
        _calls.append(json["model"])
        if json["model"] == "brand/new-live-model":
            return _FakeGroqResp(200, {"choices": [{"message": {"content": "OK from live discovery"}}]})
        return _FakeGroqResp(404, {"error": {"message": "does not exist", "code": "model_not_found"}})

    def fake_get_models(url, headers=None, timeout=None):
        return _FakeGroqResp(200, {"data": [
            {"id": "whisper-large-v3"},  # should be filtered out (not a chat model)
            {"id": "brand/new-live-model"},
        ]})

    with mock.patch.object(mod.SESSION, "post", side_effect=fake_post_all_dead), \
         mock.patch.object(mod.SESSION, "get", side_effect=fake_get_models):
        try:
            result = mod.call_groq("test prompt")
            expected_result = "OK from live discovery" if mod is not ce else "OK from live discovery"
            check(f"{label}.call_groq discovers and uses a live model when every configured model 404s",
                  result == expected_result and "brand/new-live-model" in calls)
        except Exception as e:
            check(f"{label}.call_groq live discovery fallback ({e})", False)

with mock.patch.object(pl.SESSION, "get", side_effect=RuntimeError("network down")):
    try:
        found = pl.discover_live_groq_fallback_model(exclude=set())
        check("pipeline.discover_live_groq_fallback_model degrades cleanly (empty string) on failure",
              found == "")
    except Exception as e:
        check(f"pipeline.discover_live_groq_fallback_model degrades on failure ({e})", False)


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


# --- Thumbnail-performance correlation (2026-08-18) ----------------------
# ThumbnailIdentity has been WRITTEN to VideoMeta since 2026-07-31, but
# load_video_meta() never read it back - it was silently dropped, so it
# could never be correlated against outcomes. Verifies the full path:
# load_video_meta reads it, merge_records carries it, detect_patterns
# scores it, and build_winning_content_profile surfaces the best one.
try:
    thumb_records = (
        [{**_mk_record("Topic A", "hook a", 0.9, i), "thumbnail_identity": "bold_text_closeup"} for i in range(5)]
        + [{**_mk_record("Topic B", "hook b", 0.2, i), "thumbnail_identity": "plain_frame"} for i in range(5)]
    )
    thumb_patterns, _ = wr.detect_patterns(thumb_records)
    check("weekly_review.detect_patterns adds a thumbnail_identity dimension when data exists",
          "thumbnail_identity" in thumb_patterns)
    thumb_profile = wr.build_winning_content_profile(thumb_records, thumb_patterns)
    check("weekly_review.build_winning_content_profile surfaces the best-performing thumbnail identity",
          thumb_profile.get("best_thumbnail_identity") == "bold_text_closeup")
except Exception as e:
    check(f"thumbnail-performance correlation ({e})", False)

try:
    no_thumb_patterns, _ = wr.detect_patterns([_mk_record("Topic C", "hook c", 0.5, i) for i in range(3)])
    check("weekly_review.detect_patterns skips thumbnail_identity dimension when no record has one",
          "thumbnail_identity" not in no_thumb_patterns)
except Exception as e:
    check(f"thumbnail_identity dimension gracefully skipped ({e})", False)


# --- Title style/length correlation, independent of topic (2026-08-19) ---
# Title text has been stored since day one but never correlated against
# performance independent of topic - the audit's explicitly flagged gap.
try:
    check("weekly_review.title_style_bucket classifies a question title",
          wr.title_style_bucket("Why Do We Fall For This?") == "question")
    check("weekly_review.title_style_bucket classifies a colon/subtitle title",
          wr.title_style_bucket("The Mirror Effect: Why You Copy Others") == "colon/subtitle")
    check("weekly_review.title_style_bucket classifies a number title",
          wr.title_style_bucket("5 Reasons Your Brain Lies To You") == "number")
    check("weekly_review.title_style_bucket classifies a plain statement title",
          wr.title_style_bucket("The Hidden Psychology Behind Attraction") == "plain statement")
    check("weekly_review.title_length_bucket buckets a short title",
          wr.title_length_bucket("Why You Overthink") == "short (<=6 words)")
    check("weekly_review.title_length_bucket buckets a long title",
          wr.title_length_bucket("The Hidden Psychological Reason Why You Can't Stop Thinking About That One Person") == "long (11+ words)")
except Exception as e:
    check(f"title_style_bucket/title_length_bucket ({e})", False)

try:
    title_records = (
        [{**_mk_record("Topic A", "hook a", 0.9, i), "title": "Why Does This Happen To You?"} for i in range(5)]
        + [{**_mk_record("Topic B", "hook b", 0.2, i), "title": "The Psychology Of Emotions"} for i in range(5)]
    )
    title_patterns, _ = wr.detect_patterns(title_records)
    check("weekly_review.detect_patterns adds a title_style dimension when titles exist",
          "title_style" in title_patterns)
    title_profile = wr.build_winning_content_profile(title_records, title_patterns)
    check("weekly_review.build_winning_content_profile surfaces the best-performing title style",
          title_profile.get("best_title_style") == "question")
except Exception as e:
    check(f"title style correlation end-to-end ({e})", False)

# --- ScriptStructure now actually varies (2026-08-19) --------------------
# Previously every single video logged the same hardcoded structure_tag
# ("story_short_v1"), so weekly_review.py's script_structure pattern
# dimension had zero real variation to compare - the audit's last
# remaining gap. Verifies generate_script() actually injects a different
# STRUCTURE section per structure_tag, and that the rotation/history
# helpers behave correctly.
from brand_rules import SCRIPT_STRUCTURES, SCRIPT_STRUCTURE_KEYS, pick_next_structure

try:
    check("brand_rules.SCRIPT_STRUCTURES defines more than one real variant",
          len(SCRIPT_STRUCTURE_KEYS) >= 2)
    picked = pick_next_structure(["hook_problem_reveal", "hook_problem_reveal"])
    check("brand_rules.pick_next_structure avoids repeating the last two structures",
          picked != "hook_problem_reveal")
    check("brand_rules.pick_next_structure falls back to any structure when history is empty",
          pick_next_structure([]) in SCRIPT_STRUCTURE_KEYS)
except Exception as e:
    check(f"brand_rules structure rotation ({e})", False)

captured_prompts = {}


def _fake_call_groq_capture(prompt):
    captured_prompts["last"] = prompt
    return json.dumps({
        "title": "Test Title", "description": "desc", "sentences": ["a sentence " * 3] * 16,
        "visual_keywords": ["x"] * 16, "tags": ["t"] * 10, "hook_type": "question", "cta_line": "",
    })


with mock.patch.object(pl, "call_groq", side_effect=_fake_call_groq_capture):
    try:
        pl.generate_script("Some Topic", "Social Psychology", structure_tag="hook_story_twist")
        check("pipeline.generate_script injects the hook_story_twist STRUCTURE text when requested",
              "twist" in captured_prompts["last"].lower())
        pl.generate_script("Some Topic", "Social Psychology", structure_tag="hook_question_payoff")
        check("pipeline.generate_script injects a DIFFERENT STRUCTURE text for a different tag",
              "direct payoff" in captured_prompts["last"].lower()
              or "no more stalling" in captured_prompts["last"].lower())
        pl.generate_script("Some Topic", "Social Psychology", structure_tag="not_a_real_tag")
        check("pipeline.generate_script falls back to hook_problem_reveal for an unknown/missing structure_tag",
              "curiosity gap" in captured_prompts["last"].lower())
    except Exception as e:
        check(f"pipeline.generate_script structure_tag injection ({e})", False)

with mock.patch.object(pl, "sheet_get") as msg_struct_hist:
    msg_struct_hist.return_value = [["hook_problem_reveal"], ["hook_story_twist"], ["hook_problem_reveal"]]
    try:
        recent = pl.get_recent_structures("fake-token")
        check("pipeline.get_recent_structures reads structure history from VideoMeta column H",
              recent == ["hook_problem_reveal", "hook_story_twist", "hook_problem_reveal"])
    except Exception as e:
        check(f"pipeline.get_recent_structures ({e})", False)


with mock.patch.object(pl, "sheet_get") as msg_title_profile:
    msg_title_profile.return_value = [["2026-08-19", json.dumps({
        "best_title_style": "question",
        "weak_topics": [],
    }), "x"]]
    try:
        fb = pl.build_fallback_brief_from_profile("fake-token")
        check("pipeline.build_fallback_brief_from_profile folds best_title_style into angle guidance",
              "question" in (fb.get("angle") or ""))
    except Exception as e:
        check(f"pipeline.build_fallback_brief_from_profile title style ({e})", False)

with mock.patch.object(wr, "sheet_get") as msg_meta:
    msg_meta.return_value = [
        ["vid1", "Title", "Topic", "Social Psychology", "short", "Hook text here",
         "Hook text", "hook-explain-reveal", "150", "17", "55.0", "14", "tag1,tag2",
         "2026-08-18T00:00:00+00:00", "question", "", "bold_text_closeup", "curiosity", "Sub now"],
    ]
    try:
        meta = wr.load_video_meta("fake-token")
        check("weekly_review.load_video_meta reads ThumbnailIdentity back out (previously silently dropped)",
              meta.get("vid1", {}).get("thumbnail_identity") == "bold_text_closeup")
    except Exception as e:
        check(f"load_video_meta thumbnail_identity read ({e})", False)

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


# --- Winning Content Profile now steers generation, not just topic pick --
# (2026-08-18) Previously the profile only filtered out WEAK topics; a run
# with no queued NextWeekQueue brief (or a low-scoring one) got zero benefit
# from anything weekly_review.py had learned about winning hooks/structure/
# CTA. build_fallback_brief_from_profile() closes that gap.
with mock.patch.object(pl, "sheet_get") as msg_profile:
    msg_profile.return_value = [["2026-08-17", json.dumps({
        "winning_hooks": [{"key": "There's a reason you can't stop...", "score": 0.9, "n": 5}],
        "best_structure": "hook-explain-reveal",
        "best_cta_style": "curiosity",
        "weak_topics": [],
    }), "x"]]
    try:
        fb = pl.build_fallback_brief_from_profile("fake-token")
        check("pipeline.build_fallback_brief_from_profile surfaces the top winning hook",
              "There's a reason you can't stop" in (fb.get("hook") or ""))
        check("pipeline.build_fallback_brief_from_profile surfaces the best CTA style",
              fb.get("cta_style") == "curiosity")
        check("pipeline.build_fallback_brief_from_profile surfaces the best structure",
              "hook-explain-reveal" in (fb.get("loyalty_angle") or ""))
    except Exception as e:
        check(f"pipeline.build_fallback_brief_from_profile ({e})", False)

with mock.patch.object(pl, "sheet_get") as msg_empty_profile:
    msg_empty_profile.return_value = []
    try:
        fb = pl.build_fallback_brief_from_profile("fake-token")
        check("pipeline.build_fallback_brief_from_profile returns {} when no profile exists yet", fb == {})
    except Exception as e:
        check(f"pipeline.build_fallback_brief_from_profile empty profile ({e})", False)

with mock.patch.object(pl, "get_next_queue_brief", return_value=None), \
     mock.patch.object(pl, "pick_topic_with_idea_score", return_value=("Some Topic", "Social Psychology", 8.0)), \
     mock.patch.object(pl, "sheet_get") as msg_select:
    msg_select.return_value = [["2026-08-17", json.dumps({
        "winning_hooks": [{"key": "A proven opener", "score": 0.9, "n": 5}],
        "best_structure": "hook-explain-reveal",
        "best_cta_style": "value",
        "weak_topics": [],
    }), "x"]]
    try:
        topic, pillar, score, brief = pl.select_topic_for_run("fake-token", fmt="short")
        check("pipeline.select_topic_for_run injects a profile-based fallback brief when the queue is empty",
              brief is not None and brief.get("cta_style") == "value")
    except Exception as e:
        check(f"pipeline.select_topic_for_run fallback brief injection ({e})", False)

with mock.patch.object(pl, "get_next_queue_brief", return_value=None), \
     mock.patch.object(pl, "pick_topic_with_idea_score", return_value=("Some Topic", "Social Psychology", 8.0)), \
     mock.patch.object(pl, "sheet_get", return_value=[]):
    try:
        topic, pillar, score, brief = pl.select_topic_for_run("fake-token", fmt="short")
        check("pipeline.select_topic_for_run still returns brief=None when no profile exists (unchanged behavior)",
              brief is None)
    except Exception as e:
        check(f"pipeline.select_topic_for_run no-profile behavior ({e})", False)


# --- Quality checklist logs the measured value, not just Y/N (2026-08-18) -
# Found while diagnosing a live duration_ok rejection during verification:
# the checklist log only recorded Y/N, so a rejection gave no way to tell
# how far off the render was without pulling the raw Actions log.
with mock.patch.object(pl, "sheet_append") as msa4:
    msa4.return_value = None
    try:
        result = {
            "checks": {
                "hook_ok": True, "idea_score_ok": True, "script_quality_ok": True,
                "compliance_ok": True, "duration_ok": False, "resolution_ok": True,
                "audio_ok": True, "tags_ok": True,
            },
            "failed": ["duration_ok"],
            "overall_pass": False,
            "duration": 33.7,
            "width": 1080,
            "height": 1920,
        }
        pl.log_quality_checklist("fake-token", "Some Topic", "Some Pillar", result)
        args, kwargs = msa4.call_args
        row = args[-1]
        check("pipeline.log_quality_checklist logs the measured duration in seconds, not just DurationOK=N",
              row[-1] == "33.7")
    except Exception as e:
        check(f"pipeline.log_quality_checklist measured duration ({e})", False)


# --- AI-generated topic pool replenishment (2026-08-19) -----------------
# Closes the last audit-flagged gap: TOPIC_POOL is finite and will run out
# at the current publish cadence; after that pick_topic() used to silently
# recycle already-used topics forever.
with mock.patch.object(pl, "call_groq", return_value=json.dumps({"topic": "Why We Trust Confident People More"})):
    try:
        topic = pl.generate_fresh_topic("Social Psychology", {"some old topic"})
        check("pipeline.generate_fresh_topic returns a Groq-generated topic string",
              topic == "Why We Trust Confident People More")
    except Exception as e:
        check(f"pipeline.generate_fresh_topic happy path ({e})", False)

with mock.patch.object(pl, "call_groq", return_value="not valid json"):
    try:
        topic = pl.generate_fresh_topic("Social Psychology", set())
        check("pipeline.generate_fresh_topic degrades to '' on malformed Groq output, never raises",
              topic == "")
    except Exception as e:
        check(f"pipeline.generate_fresh_topic malformed-output degrade ({e})", False)

with mock.patch.object(pl, "call_groq", side_effect=RuntimeError("groq down")):
    try:
        topic = pl.generate_fresh_topic("Social Psychology", set())
        check("pipeline.generate_fresh_topic degrades to '' when call_groq raises, never raises",
              topic == "")
    except Exception as e:
        check(f"pipeline.generate_fresh_topic exception degrade ({e})", False)

with mock.patch.object(pl, "call_groq", return_value=json.dumps({"topic": "Some Old Topic"})):
    try:
        topic = pl.generate_fresh_topic("Social Psychology", {"some old topic"})
        check("pipeline.generate_fresh_topic rejects a topic that duplicates an already-used one (case-insensitive)",
              topic == "")
    except Exception as e:
        check(f"pipeline.generate_fresh_topic dedup-against-avoid_topics ({e})", False)

all_used_rows = [[t] for t, _ in pl.TOPIC_POOL]
with mock.patch.object(pl, "sheet_get", return_value=all_used_rows), \
     mock.patch.object(pl, "load_weak_topics", return_value=set()), \
     mock.patch.object(pl, "generate_fresh_topic", return_value="A Brand New Fresh Topic") as gft:
    try:
        topic, pillar = pl.pick_topic("fake-token")
        check("pipeline.pick_topic calls generate_fresh_topic() once TOPIC_POOL is fully exhausted, and returns it",
              topic == "A Brand New Fresh Topic" and gft.called)
    except Exception as e:
        check(f"pipeline.pick_topic exhausted-pool -> fresh topic ({e})", False)

with mock.patch.object(pl, "sheet_get", return_value=all_used_rows), \
     mock.patch.object(pl, "load_weak_topics", return_value=set()), \
     mock.patch.object(pl, "generate_fresh_topic", return_value=""):
    try:
        topic, pillar = pl.pick_topic("fake-token")
        check("pipeline.pick_topic falls back to recycling the static pool when generate_fresh_topic() fails",
              any(topic == t for t, _ in pl.TOPIC_POOL))
    except Exception as e:
        check(f"pipeline.pick_topic exhausted-pool -> recycle fallback ({e})", False)

with mock.patch.object(pl, "sheet_get", return_value=[]), \
     mock.patch.object(pl, "load_weak_topics", return_value=set()), \
     mock.patch.object(pl, "generate_fresh_topic") as gft_unused:
    try:
        topic, pillar = pl.pick_topic("fake-token")
        check("pipeline.pick_topic does NOT call generate_fresh_topic() while the static pool still has candidates",
              not gft_unused.called and any(topic == t for t, _ in pl.TOPIC_POOL))
    except Exception as e:
        check(f"pipeline.pick_topic non-exhausted pool skips generation ({e})", False)


# --- Assumptive second-person hook style (2026-08-19, from trend research) -
# Weekly trend research found direct "you already do this" hooks outperform
# question-based hooks (removes the viewer's mental opt-out). Prompt now
# instructs this as the preferred style, and "assumptive" is a trackable
# hook_type so weekly_review can eventually correlate it against retention.
with mock.patch.object(pl, "call_groq", side_effect=_fake_call_groq_capture):
    try:
        pl.generate_script("Some Topic", "Social Psychology")
        check("pipeline.generate_script prompt instructs the assumptive second-person hook style",
              "assumptive" in captured_prompts["last"].lower()
              and "opt out" in captured_prompts["last"].lower())
    except Exception as e:
        check(f"pipeline.generate_script assumptive hook style prompt ({e})", False)


def _fake_call_groq_assumptive(prompt):
    return json.dumps({
        "title": "Test Title", "description": "desc", "sentences": ["a sentence " * 3] * 16,
        "visual_keywords": ["x"] * 16, "tags": ["t"] * 10, "hook_type": "assumptive", "cta_line": "",
    })


with mock.patch.object(pl, "call_groq", side_effect=_fake_call_groq_assumptive):
    try:
        script = pl.generate_script("Some Topic", "Social Psychology")
        check("pipeline.generate_script accepts 'assumptive' as a valid hook_type (not coerced to unclassified)",
              script.get("hook_type") == "assumptive")
    except Exception as e:
        check(f"pipeline.generate_script assumptive hook_type acceptance ({e})", False)


# --- summary -----------------------------------------------------------
print("\n=== SUMMARY ===")
n_pass = sum(1 for _, ok in results if ok)
for name, ok in results:
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
print(f"\n{n_pass}/{len(results)} checks passed")
sys.exit(0 if n_pass == len(results) else 1)
