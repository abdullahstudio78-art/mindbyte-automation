"""
MindByte Automation - weekly Facebook self-improving analysis (added 2026-08-19).

The Facebook-side counterpart to weekly_review.py, closing the same kind of
loop for the Facebook Page instead of the YouTube channel:

    Post to Facebook (facebook_pipeline.py / pipeline.py's cross-post)
      -> daily analytics snapshot (facebook_analytics.py)
      -> THIS SCRIPT, weekly: find winning/losing patterns
      -> write next-week content briefs to NextWeekQueue (Format="facebook")
      -> select_topic_for_run(fmt="facebook") in facebook_pipeline.py and
         pipeline.py automatically consumes them next run
      -> repeat.

Deliberately simpler than weekly_review.py's rank-based/recency-weighted
system: Facebook will have far less data per week initially (a handful of
posts vs. 14+ Shorts/week), so a lighter grouped-average approach with an
explicit "not enough data" guard is more honest than a heavier statistical
model that would just be overfitting noise. As real weekly volume builds up
(5/day = 35/week once publish_facebook.yml is live), this can be upgraded
to match weekly_review.py's approach if the extra sophistication earns its
keep - not assumed necessary on day one.

Run weekly via .github/workflows/facebook_weekly_review.yml (Sundays, after
a week of facebook_analytics.py snapshots have accumulated).
"""

import json
import os
from collections import defaultdict
from datetime import datetime, timezone

import pipeline as p

MIN_GROUP_SAMPLE = 2  # same "don't call it a pattern off one video" guard as weekly_review.py

# How many new Facebook-tagged briefs to queue each week. Sized against the
# 5/day = 35/week target: not 35 (that would just be guessing 35 topics
# blind with no performance signal to weight them), but enough that the
# queue doesn't run dry mid-week and fall back to blind random picks for
# every post. Revisit once real weekly volume/data is in and the "not
# enough data yet" gate below stops firing.
NEW_FACEBOOK_BRIEFS_PER_WEEK = 15

FACEBOOK_VIDEO_META_RANGE = "FacebookVideoMeta!A:N"
FACEBOOK_ANALYTICS_HISTORY_RANGE = "FacebookAnalyticsHistory!A:N"

NEXT_QUEUE_HEADER = [
    "WeekOf", "Format", "Pillar", "Title", "Hook", "Angle",
    "SEOTitle", "SEODescription", "SEOTags", "CTAStyle",
    "TargetLengthSec", "Used", "CreatedAt",
    "HookType", "Series", "ThumbnailConcept", "ChapterOutline",
    "LoyaltyAngle", "Confidence",
]

FACEBOOK_WEEKLY_PLAN_TAB = "FacebookWeeklyPlan"
FACEBOOK_WEEKLY_PLAN_HEADER = [
    "WeekOf", "PostsAnalyzed", "TopPattern", "WeakPattern", "PlanNotes", "GeneratedAt",
]

# Added 2026-09-03 - real-world-psychology build-order item #3 (analytics
# extension): this was the one real gap left after confirming the rest of
# "Self-Improving System v2" (see claude/self-improving-system-v2-
# implementation.md) was already live - this file had its OWN confidence-
# scored weekly review (see build_pattern_summary below), but it never fed
# into the SAME consolidated report weekly_review.py already writes for
# YouTube. Rather than build a second reporting system, this writes
# Facebook's sections into weekly_review.py's own WEEKLY_REPORT_FULL_TAB
# under a "facebook_"-prefixed section name, same WeekOf - so a human (or
# a future system) reading WeeklyReportFull sees both platforms for the
# same week in one place, not two separate reports to cross-reference by
# hand. Tab name/header duplicated here (not imported from weekly_review.py)
# on purpose - this file already duplicates NEXT_QUEUE_HEADER the same way,
# matching this codebase's existing pattern of small constant duplication
# over a cross-import between the two weekly-review scripts.
WEEKLY_REPORT_FULL_TAB = "WeeklyReportFull"
WEEKLY_REPORT_FULL_HEADER = ["Date", "Section", "Content"]


def write_facebook_report_sections(token: str, week_of: str, sections: dict) -> None:
    """Best-effort: appends Facebook's weekly findings into the SAME shared
    WeeklyReportFull tab the YouTube side writes to, one row per section,
    prefixed so they're clearly distinguishable at a glance. Never raises -
    this is a reporting nicety, not something worth risking the rest of
    this script's real work (queuing next week's briefs) over."""
    written = 0
    for section, content in sections.items():
        row = [week_of, f"facebook_{section}", content]
        try:
            p.sheet_append(token, f"{WEEKLY_REPORT_FULL_TAB}!A:C", row)
            written += 1
        except Exception:
            try:
                if p.ensure_sheet_tab(token, WEEKLY_REPORT_FULL_TAB, WEEKLY_REPORT_FULL_HEADER):
                    p.sheet_append(token, f"{WEEKLY_REPORT_FULL_TAB}!A:C", row)
                    written += 1
            except Exception as e:  # noqa: BLE001 - reporting must never abort the run
                print(f"[facebook_weekly] could not write WeeklyReportFull section '{section}': {e}")
    # Explicit success confirmation (2026-09-03: the first live verification
    # run of this function had zero failure output, which is ambiguous on
    # its own - "silent" could mean "wrote fine" or "never ran". Every other
    # writer in this codebase logs a positive confirmation; this one should
    # too, so a future run's log is provable either way instead of inferred.
    print(f"[facebook_weekly] wrote {written}/{len(sections)} WeeklyReportFull section rows "
          f"(facebook_* prefix) for week {week_of}")


def safe_num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def load_facebook_performance(token: str) -> list:
    """Joins FacebookVideoMeta (structural attributes, one row per post)
    with the LATEST FacebookAnalyticsHistory snapshot per video (so a video
    with several days of snapshots contributes its most recent numbers,
    not every historical day as a separate 'video')."""
    meta_rows = p.sheet_get(token, FACEBOOK_VIDEO_META_RANGE)
    hist_rows = p.sheet_get(token, FACEBOOK_ANALYTICS_HISTORY_RANGE)
    if meta_rows and meta_rows[0] and meta_rows[0][0] == "FacebookVideoID":
        meta_rows = meta_rows[1:]
    if hist_rows and hist_rows[0] and hist_rows[0][0] == "Date":
        hist_rows = hist_rows[1:]

    latest_by_id = {}
    for row in hist_rows:
        row = row + [""] * (13 - len(row))
        date, video_id = row[0], row[1]
        if not video_id:
            continue
        prev = latest_by_id.get(video_id)
        if prev is None or date >= prev["date"]:
            latest_by_id[video_id] = {
                "date": date, "plays": safe_num(row[5]),
                "likes": safe_num(row[8]), "comments": safe_num(row[9]), "shares": safe_num(row[10]),
            }

    videos = []
    for row in meta_rows:
        row = row + [""] * (14 - len(row))
        video_id, title, topic, pillar, hook_text, hook_opener = row[0], row[1], row[2], row[3], row[4], row[5]
        structure_tag, word_count, sentence_count, length_sec = row[6], row[7], row[8], row[9]
        post_hour, tags_raw, cta_style = row[10], row[11], row[12]
        if not video_id:
            continue
        stats = latest_by_id.get(video_id, {})
        plays, likes, comments, shares = (
            stats.get("plays", 0), stats.get("likes", 0), stats.get("comments", 0), stats.get("shares", 0),
        )
        # Reach-weighted engagement score: shares/comments are much stronger
        # distribution signals than plays alone (a share/comment puts the
        # Reel in front of a NEW audience; a play doesn't), so they're
        # weighted up rather than just averaging raw counts.
        score = plays + likes * 3 + comments * 5 + shares * 8
        videos.append({
            "video_id": video_id, "title": title, "topic": topic, "pillar": pillar,
            "hook_opener": hook_opener, "structure": structure_tag,
            "word_count": safe_num(word_count), "length_sec": safe_num(length_sec),
            "post_hour": post_hour, "tags": [t.strip() for t in tags_raw.split(",") if t.strip()],
            "cta_style": cta_style, "plays": plays, "likes": likes, "comments": comments,
            "shares": shares, "score": score,
        })
    return videos


def group_pattern(videos: list, key_fn, label: str) -> dict:
    """Grouped average score by key_fn(video); best/worst group, only if
    both have at least MIN_GROUP_SAMPLE videos - otherwise flagged as not
    enough data, same convention as weekly_review.py."""
    groups = defaultdict(list)
    for v in videos:
        key = key_fn(v)
        if key:
            groups[key].append(v["score"])
    qualifying = {k: vs for k, vs in groups.items() if len(vs) >= MIN_GROUP_SAMPLE}
    if len(qualifying) < 2:
        return {"label": label, "enough_data": False}
    averages = {k: sum(vs) / len(vs) for k, vs in qualifying.items()}
    best_key = max(averages, key=averages.get)
    worst_key = min(averages, key=averages.get)
    return {
        "label": label, "enough_data": True,
        "best": {"key": best_key, "avg_score": round(averages[best_key], 1), "n": len(qualifying[best_key])},
        "worst": {"key": worst_key, "avg_score": round(averages[worst_key], 1), "n": len(qualifying[worst_key])},
    }


def _word_count_band(word_count: float) -> str:
    if word_count < 80:
        return "short (<80 words)"
    if word_count < 140:
        return "medium (80-140 words)"
    return "long (140+ words)"


def build_pattern_summary(videos: list) -> list:
    patterns = [
        group_pattern(videos, lambda v: v["pillar"], "pillar"),
        group_pattern(videos, lambda v: v["hook_opener"], "hook_opener"),
        group_pattern(videos, lambda v: v["structure"], "script_structure"),
        group_pattern(videos, lambda v: v["cta_style"], "cta_style"),
        group_pattern(videos, lambda v: str(v["post_hour"]), "post_hour_utc"),
        group_pattern(videos, lambda v: _word_count_band(v["word_count"]), "word_count_band"),
    ]
    # Per-tag pattern (a video can carry multiple tags, unlike the fields above)
    tag_groups = defaultdict(list)
    for v in videos:
        for tag in v["tags"]:
            tag_groups[tag].append(v["score"])
    qualifying_tags = {t: vs for t, vs in tag_groups.items() if len(vs) >= MIN_GROUP_SAMPLE}
    if len(qualifying_tags) >= 2:
        tag_averages = {t: sum(vs) / len(vs) for t, vs in qualifying_tags.items()}
        best_tag = max(tag_averages, key=tag_averages.get)
        worst_tag = min(tag_averages, key=tag_averages.get)
        patterns.append({
            "label": "seo_tag", "enough_data": True,
            "best": {"key": best_tag, "avg_score": round(tag_averages[best_tag], 1), "n": len(qualifying_tags[best_tag])},
            "worst": {"key": worst_tag, "avg_score": round(tag_averages[worst_tag], 1), "n": len(qualifying_tags[worst_tag])},
        })
    else:
        patterns.append({"label": "seo_tag", "enough_data": False})
    return patterns


def generate_facebook_briefs(videos: list, patterns: list, count: int) -> list:
    """Asks Groq for `count` next-video briefs in the same shape
    weekly_review.py's NextWeekQueue rows use, informed by this week's
    Facebook-specific patterns. Falls back to an empty list (pure no-op,
    facebook_pipeline.py's normal idea-scored random pick keeps working)
    on any Groq failure - this must never block anything."""
    top_videos = sorted(videos, key=lambda v: v["score"], reverse=True)[:5]
    bottom_videos = sorted(videos, key=lambda v: v["score"])[:5]
    pattern_lines = []
    for pat in patterns:
        if not pat.get("enough_data"):
            pattern_lines.append(f"- {pat['label']}: not enough data yet")
            continue
        pattern_lines.append(
            f"- {pat['label']}: best='{pat['best']['key']}' (avg score {pat['best']['avg_score']}, "
            f"n={pat['best']['n']}) vs worst='{pat['worst']['key']}' "
            f"(avg score {pat['worst']['avg_score']}, n={pat['worst']['n']})"
        )

    prompt = f"""You are the growth strategist for "MindByte", a psychology/human-behavior
Facebook Reels page (pillars: Relationship Psychology, Human Behavior Psychology,
Social Psychology, Emotional Intelligence). Below is this week's Facebook Reels
performance data (score = plays + likes*3 + comments*5 + shares*8, weighted toward
distribution signals since shares/comments spread a Reel to new audiences).

TOP PERFORMERS:
{json.dumps(top_videos, indent=2)}

BOTTOM PERFORMERS:
{json.dumps(bottom_videos, indent=2)}

PATTERNS DETECTED THIS WEEK:
{chr(10).join(pattern_lines)}

Generate exactly {count} next-video content briefs for Facebook Reels specifically
(NOT YouTube - lean into what drives comments/shares/replays on Facebook: relatable
questions, "tag someone who...", debate-starting claims, surprising reversals),
informed by the patterns above. Skip any dimension marked "not enough data yet"
rather than inventing a reason from it.

Respond with ONLY a JSON object: {{"next_ideas": [{{"format": "facebook", "pillar": "...",
"title": "...", "hook": "...", "angle": "...", "seo_title": "...", "seo_description": "...",
"seo_tags": ["...", "..."], "cta_style": "...", "target_length_sec": 60,
"hook_type": "...", "series": "", "thumbnail_concept": "", "chapter_outline": [],
"loyalty_angle": "", "confidence": "Low|Medium|High"}}, ...]}}"""

    try:
        raw = p.call_groq(prompt)
        data = json.loads(raw)
        ideas = data.get("next_ideas", [])
        for idea in ideas:
            idea["format"] = "facebook"
        return ideas
    except Exception as e:  # noqa: BLE001 - must never block the run
        print(f"[facebook_weekly] brief generation failed, skipping this week's queue additions: {e}")
        return []


def main() -> None:
    access_token = p.get_access_token()
    videos = load_facebook_performance(access_token)
    week_of = datetime.now(timezone.utc).date().isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()

    if len(videos) < MIN_GROUP_SAMPLE:
        print(f"[facebook_weekly] only {len(videos)} Facebook video(s) with data so far - "
              f"not enough to find patterns yet, skipping this week's review")
        return

    patterns = build_pattern_summary(videos)
    for pat in patterns:
        if pat.get("enough_data"):
            print(f"[facebook_weekly] pattern - {pat['label']}: best={pat['best']} worst={pat['worst']}")
        else:
            print(f"[facebook_weekly] pattern - {pat['label']}: not enough data yet")

    ideas = generate_facebook_briefs(videos, patterns, NEW_FACEBOOK_BRIEFS_PER_WEEK)

    top = sorted(videos, key=lambda v: v["score"], reverse=True)[:3]
    bottom = sorted(videos, key=lambda v: v["score"])[:3]
    plan_row = [
        week_of, len(videos),
        "; ".join(f"{v['title']} (score {v['score']:.0f})" for v in top),
        "; ".join(f"{v['title']} (score {v['score']:.0f})" for v in bottom),
        f"{len(ideas)} new Facebook briefs queued this week from {len(videos)} analyzed posts",
        now_iso,
    ]
    # Fold into the SAME consolidated weekly report the YouTube side gets
    # (see write_facebook_report_sections' docstring above for why this
    # lives here instead of a second, separate report).
    pattern_lines = []
    for pat in patterns:
        if pat.get("enough_data"):
            pattern_lines.append(f"{pat['label']}: best={pat['best']} worst={pat['worst']}")
        else:
            pattern_lines.append(f"{pat['label']}: not enough data yet")
    write_facebook_report_sections(access_token, week_of, {
        "executive_summary": (
            f"{len(videos)} Facebook posts analyzed this week. "
            f"Top performer: {top[0]['title'] if top else 'n/a'} (score {top[0]['score']:.0f})." if top
            else f"{len(videos)} Facebook posts analyzed this week."
        ),
        "biggest_wins": "; ".join(f"{v['title']} (score {v['score']:.0f})" for v in top),
        "biggest_failures": "; ".join(f"{v['title']} (score {v['score']:.0f})" for v in bottom),
        "pattern_summary": " | ".join(pattern_lines),
        "briefs_queued": f"{len(ideas)} new Facebook briefs queued this week from {len(videos)} analyzed posts",
    })

    try:
        p.sheet_append(access_token, f"{FACEBOOK_WEEKLY_PLAN_TAB}!A:F", plan_row)
    except Exception:
        try:
            if p.ensure_sheet_tab(access_token, FACEBOOK_WEEKLY_PLAN_TAB, FACEBOOK_WEEKLY_PLAN_HEADER):
                p.sheet_append(access_token, f"{FACEBOOK_WEEKLY_PLAN_TAB}!A:F", plan_row)
        except Exception as e:  # noqa: BLE001
            print(f"[facebook_weekly] could not write FacebookWeeklyPlan row: {e}")
    print("[facebook_weekly] FacebookWeeklyPlan row written")

    written = 0
    for idea in ideas:
        row = [
            week_of, "facebook", idea.get("pillar", ""), idea.get("title", ""),
            idea.get("hook", ""), idea.get("angle", ""), idea.get("seo_title", ""),
            idea.get("seo_description", ""), ", ".join(idea.get("seo_tags", []) or []),
            idea.get("cta_style", ""), idea.get("target_length_sec", ""), "N", now_iso,
            idea.get("hook_type", "unclassified"), idea.get("series", ""),
            idea.get("thumbnail_concept", ""), "; ".join(idea.get("chapter_outline", []) or []),
            idea.get("loyalty_angle", ""), idea.get("confidence", "Low"),
        ]
        try:
            p.sheet_append(access_token, "NextWeekQueue!A:S", row)
            written += 1
        except Exception:
            try:
                if p.ensure_sheet_tab(access_token, "NextWeekQueue", NEXT_QUEUE_HEADER):
                    p.sheet_append(access_token, "NextWeekQueue!A:S", row)
                    written += 1
            except Exception as e:  # noqa: BLE001
                print(f"[facebook_weekly] could not write NextWeekQueue row: {e}")

    print(f"[facebook_weekly] {written} Facebook briefs written to NextWeekQueue")
    print("[facebook_weekly] done")


if __name__ == "__main__":
    main()
