"""
MindByte Automation - Facebook-only Reels pipeline (added 2026-08-19).

Why this file exists: the user asked for 5 Facebook Reels/day, decoupled
from YouTube's own 2/day schedule. 2 of the 5 already happen "for free" as
a byproduct of publish.yml's existing Facebook cross-post block in
pipeline.py's main() (it piggybacks on the same script/topic/footage as
that run's YouTube Short). The other 3/day run from THIS file, on their
own schedule (.github/workflows/publish_facebook.yml), with no YouTube
upload involved at all - own topic pick, own script, own footage, own
voiceover, own Facebook-branded render, own post.

Deliberately reuses pipeline.py's building blocks (imported as `p` below)
instead of reimplementing them - topic selection, script generation,
footage search, voiceover synthesis, music, branding assets, video
assembly, and the Sheets/Groq helpers all come straight from pipeline.py,
so a fix or improvement made there (e.g. a new script structure, a Groq
model change, a footage-source fix) applies here automatically with zero
duplication to keep in sync.

Feeds the Facebook growth loop end to end:
  facebook_pipeline.py (this file, posts + logs FacebookVideoMeta)
    -> facebook_analytics.py (daily, pulls Facebook Insights per post)
    -> facebook_weekly_review.py (weekly, finds patterns, writes briefs)
    -> back into select_topic_for_run(fmt="facebook") here and in
       pipeline.py's cross-post block, closing the loop.

Every failure mode here is best-effort / non-fatal in the same spirit as
the rest of the codebase - a failed run should exit cleanly and print why,
never leave a broken partial post or a hung Action.
"""

import os
import tempfile
from datetime import datetime, timezone

import pipeline as p
from brand_rules import pick_next_cta_style, pick_next_structure
from facebook_publish import facebook_configured, post_short_to_facebook

FACEBOOK_VIDEO_META_TAB = "FacebookVideoMeta"
FACEBOOK_VIDEO_META_RANGE = "FacebookVideoMeta!A:N"
FACEBOOK_VIDEO_META_HEADER = [
    "FacebookVideoID", "Title", "Topic", "Pillar", "HookText", "HookOpenerWords",
    "ScriptStructure", "WordCount", "SentenceCount", "VideoLengthSec",
    "PostHourUTC", "Tags", "CTAStyle", "CreatedAt",
]


def log_facebook_video_meta(
    access_token: str, video_id: str, title: str, topic: str, pillar: str,
    script: dict, structure_tag: str, video_length_sec: float, cta_style: str,
) -> None:
    """Best-effort logging of one row per Facebook post, same self-healing
    pattern as pipeline.py's log_video_meta(). This is what makes
    facebook_analytics.py's per-video Insights pull possible at all (it
    needs a list of FacebookVideoIDs to look up) and what lets
    facebook_weekly_review.py correlate performance against pillar/hook/
    structure/length/tags, same as the YouTube side already does."""
    hook_text = script["sentences"][0] if script.get("sentences") else ""
    hook_opener = " ".join((hook_text or "").split()[:6])
    word_count = sum(len(s.split()) for s in script.get("sentences", []))
    row = [
        video_id, title, topic, pillar, hook_text, hook_opener,
        structure_tag, word_count, len(script.get("sentences", [])),
        round(video_length_sec, 1), datetime.now(timezone.utc).hour,
        ", ".join(script.get("tags", [])), cta_style,
        datetime.now(timezone.utc).isoformat(),
    ]
    try:
        p.sheet_append(access_token, FACEBOOK_VIDEO_META_RANGE, row)
    except Exception as e:  # noqa: BLE001 - logging must never abort a successful post
        healed = False
        try:
            healed = p.ensure_sheet_tab(access_token, FACEBOOK_VIDEO_META_TAB, FACEBOOK_VIDEO_META_HEADER)
            if healed:
                p.sheet_append(access_token, FACEBOOK_VIDEO_META_RANGE, row)
        except Exception:
            healed = False
        if not healed:
            print(f"[facebook_pipeline] could not log FacebookVideoMeta (does the tab exist yet?): {e}")


def main() -> None:
    if not facebook_configured():
        print("[facebook_pipeline] skipped: Facebook secrets not configured yet")
        return

    access_token = p.get_access_token()

    # fmt="facebook" reads from the SAME NextWeekQueue tab select_topic_for_run()
    # already uses for Shorts/longform, just filtered to rows facebook_weekly_review.py
    # writes with Format="facebook" - falls back to the normal idea-scored
    # random pick automatically when no Facebook brief is queued yet (e.g.
    # the first week, before any Facebook analytics/review has run).
    topic, pillar, idea_score_avg, brief = p.select_topic_for_run(access_token, fmt="facebook")
    print(f"[facebook_pipeline] topic: {topic} (pillar: {pillar}) - idea score avg {idea_score_avg:.1f}"
          + (" [from Facebook weekly self-improvement queue]" if brief else ""))

    cta_style = pick_next_cta_style(p.get_recent_cta_styles(access_token))
    structure_tag = pick_next_structure(p.get_recent_structures(access_token))
    print(f"[facebook_pipeline] CTA style: {cta_style}, structure: {structure_tag}")

    script, quality = p.generate_and_score_script(
        topic, pillar, brief=brief, cta_style=cta_style, structure_tag=structure_tag,
    )
    print(f"[facebook_pipeline] title: {script['title']}")
    print(f"[facebook_pipeline] final quality score: {quality['score']} - {quality['notes']}")

    compliance = p.compliance_check(script)
    print(f"[facebook_pipeline] compliance: {compliance}")
    if quality["score"] < p.QUALITY_THRESHOLD or not compliance["passed"]:
        print("[facebook_pipeline] rejected by quality/compliance gate - no post")
        return

    with tempfile.TemporaryDirectory() as workdir:
        storyboard = p.generate_storyboard(script.get("sentences") or [])
        footage_queries = [
            (beat.get("footage_query") or "").strip() or script["visual_keywords"][i]
            for i, beat in enumerate(storyboard)
        ] if storyboard else script["visual_keywords"]

        # Same "continue the last shot for the CTA beat" approach as
        # pipeline.py's Facebook cross-post block, and the same native
        # like/share/follow closing line instead of YouTube's "Subscribe"
        # framing (Facebook has no subscribe concept).
        fb_cta_line = p.pick_facebook_cta_line()
        spoken_sentences = list(script["sentences"]) + [fb_cta_line]
        footage_queries = list(footage_queries) + [
            footage_queries[-1] if footage_queries else script["title"]
        ]

        clip_paths, stock_attributions = p.gather_clips(
            footage_queries, workdir, sentences=spoken_sentences,
        )
        if not clip_paths:
            print("[facebook_pipeline] no usable stock clips found - aborting")
            return

        audio_path, segment_durations = p.generate_voiceover_segments(spoken_sentences, workdir, pillar)
        audio_duration = p.ffprobe_duration(audio_path)

        final_audio_path = audio_path
        description = script["description"]
        for attribution in stock_attributions:
            if attribution not in description:
                description += f"\n\n{attribution}"

        music_path = os.path.join(workdir, "music.mp3")
        music_meta = p.fetch_background_music(music_path, pillar)
        if music_meta:
            mixed_path = os.path.join(workdir, "voiceover_mixed.mp3")
            try:
                p.mix_background_music(audio_path, music_path, audio_duration, mixed_path)
                final_audio_path = mixed_path
                print(f"[facebook_pipeline] music: '{music_meta['title']}' by "
                      f"{music_meta['creator']} ({music_meta['license']})")
            except Exception as e:  # noqa: BLE001 - music mix must never abort the run
                print(f"[facebook_pipeline] music mix failed, continuing without music: {e}")

        mastered_audio_path = os.path.join(workdir, "voiceover_mastered.mp3")
        try:
            p.master_audio(final_audio_path, mastered_audio_path, audio_duration)
            final_audio_path = mastered_audio_path
        except Exception as e:  # noqa: BLE001 - mastering must never abort the run
            print(f"[facebook_pipeline] audio mastering failed, continuing unmastered: {e}")

        badge_path = os.path.join(workdir, "fb_badge.png")
        try:
            p.build_subscribe_badge(badge_path, pillar, headline="LIKE + SHARE", subline="Follow @MindByte")
        except Exception as e:  # noqa: BLE001 - branding must never abort the run
            print(f"[facebook_pipeline] end card failed, continuing without it: {e}")
            badge_path = None

        watermark_path = os.path.join(workdir, "watermark.png")
        try:
            p.build_watermark_png(watermark_path)
        except Exception as e:  # noqa: BLE001 - branding must never abort the run
            print(f"[facebook_pipeline] watermark failed, continuing without it: {e}")
            watermark_path = None

        ass_path = os.path.join(workdir, "captions.ass")
        p.build_ass(spoken_sentences, segment_durations, ass_path)

        output_path = os.path.join(workdir, "facebook_final.mp4")
        p.assemble_video(
            clip_paths, segment_durations, final_audio_path, ass_path, output_path,
            storyboard=storyboard,
            title_card_path=None, watermark_path=watermark_path, subscribe_badge_path=badge_path,
        )

        try:
            video_info = p.ffprobe_video_info(output_path)
            video_length_sec = video_info.get("duration") or audio_duration
        except Exception:  # noqa: BLE001 - metadata-only, never worth aborting over
            video_length_sec = audio_duration

        result = post_short_to_facebook(output_path, script["title"], description, script.get("tags", []))
        if result["status"] == "skipped":
            print(f"[facebook_pipeline] skipped ({result['reason']})")
            return
        if not result["posted"]:
            print(f"[facebook_pipeline] not posted - status={result['status']} reason={result['reason']}")
            return

        print(f"[facebook_pipeline] posted (video_id={result['video_id']})")
        log_facebook_video_meta(
            access_token, result["video_id"], script["title"], topic, pillar,
            script, structure_tag, video_length_sec, cta_style,
        )

    print("[facebook_pipeline] done")


if __name__ == "__main__":
    main()
