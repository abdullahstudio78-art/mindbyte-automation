"""
MindByte Automation - long-form (phase 4) content pipeline.

This is a STANDALONE companion to pipeline.py (the Shorts pipeline), not a
replacement for it. It targets an 8-15 minute landscape (1920x1080) video
instead of a 30-60 second vertical Short.

Why a separate file instead of extending pipeline.py in place:
  - The whole B-roll/voiceover chunking strategy is different by design.
    The Shorts pipeline maps ONE Pexels clip + ONE edge-tts call to EVERY
    SENTENCE (gather_clips / generate_voiceover_segments in pipeline.py).
    Doing that for an 1800-word long-form script would mean ~120+ Pexels
    calls and ~120+ edge-tts calls per run - Pexels' free tier is 200
    requests/hour, and edge-tts has no published rate limit but is known
    to throttle under heavy per-call load. So long-form B-roll/TTS here is
    chunked per PARAGRAPH instead: each paragraph gets one TTS call and a
    handful of Pexels clips (see LF_MAX_CLIPS_PER_PARAGRAPH), keeping a
    ~12-paragraph video around 30-50 Pexels calls, well inside the free
    limit even with several test runs in the same hour. This was agreed
    with the channel owner in the phase-4 feasibility discussion
    (2026-07-19) before this file was written.
  - Landscape video needs different caption positioning (no Shorts mobile
    UI overlay to avoid) and a different Pexels orientation, so reusing
    pipeline.py's build_ass()/assemble_video()/search_pexels_clip() as-is
    isn't possible - those hardcode portrait 1080x1920.

Everything that ISN'T format-specific (Google OAuth/Sheets helpers, Groq
call plumbing, topic picking, background music, per-clip TTS synthesis,
word highlighting, YouTube upload, ffprobe helpers) is imported directly
from pipeline.py rather than duplicated, so a fix made there (e.g. the
MUSIC_VOLUME tweak) doesn't have to be re-applied here by hand.

Standalone by design (per the channel owner's "test one thing at a time"
approach): this file is only ever triggered manually via
.github/workflows/publish_longform.yml's workflow_dispatch. It must NOT be
wired into any schedule: cron until a human has reviewed at least one
full test run end-to-end, mirroring exactly how the Shorts pipeline's
phases were validated before daily automation was turned on.
"""

import asyncio
import json
import os
import random
import re
import subprocess
import tempfile
import textwrap
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageDraw

from pipeline import (
    # Config / shared session
    PEXELS_API_KEY,
    PUBLISH_DELAY_HOURS,
    QUALITY_THRESHOLD,
    IDEA_SCORE_AVG_THRESHOLD,
    CONTENT_PILLARS,
    SESSION,
    # Google OAuth / Sheets
    get_access_token,
    sheet_append,
    # Topic selection (shared pool + shared UsedTopics tab across both
    # Shorts and long-form - a topic covered in either format is still
    # "spent" for a while, which is good content strategy, not a bug)
    pick_topic_with_idea_score,
    mark_topic_used,
    # Groq
    call_groq,
    # Pexels / downloads
    download_file,
    FALLBACK_QUERIES,
    # Background music (format-agnostic - already trims/loops to whatever
    # duration is passed, so it works unchanged for a long-form track)
    fetch_background_music,
    mix_background_music,
    # Voiceover synthesis primitives
    _synthesize,
    _sentence_prosody,
    ffprobe_duration,
    # Caption word-highlighting (format-agnostic string -> ASS text)
    _highlight_ass_text,
    # YouTube upload + technical video probing (format-agnostic)
    upload_to_youtube,
    ffprobe_video_info,
    set_youtube_thumbnail,
    # Branding (Phase 4 polish pass, 2026-07-20) - landscape title
    # card/thumbnail built locally in this module, but the shared
    # low-level drawing helpers and brand constants are reused as-is
    PILLAR_ACCENT_COLORS,
    BRAND_BG_TOP,
    BRAND_BG_BOTTOM,
    BRAND_TEXT_COLOR,
    BRAND_NAME,
    _brand_font,
    _draw_logo_mark,
    build_watermark_png,
)

# ---------------------------------------------------------------------------
# Long-form specific config
# ---------------------------------------------------------------------------

LF_VIDEO_WIDTH = 1920
LF_VIDEO_HEIGHT = 1080

LF_MIN_WORDS = 1400          # ~8-9 min spoken at edge-tts's typical pace
LF_TARGET_WORDS = 1700
LF_MAX_WORDS = 2100          # ~13-15 min - keep a margin under the 15 min ceiling

# Run #1 (2026-07-19, commit b85eae6) found that Groq (llama-3.3-70b) reliably
# writes toward the LOW end of whatever paragraph-count range it's given, and
# largely ignores an explicit "120-190 words per paragraph" instruction -
# real observed output averaged ~87 words/paragraph across all 3 attempts,
# producing 9, 10, and 10 paragraphs (all near the old floor of 9) and only
# 784-877 total words - nowhere near the 1400 word floor, even after
# corrective feedback asking for more words. Rather than fight the model's
# demonstrated per-paragraph length, the floor here is raised to lean on the
# lever it actually respects (paragraph COUNT), sized off that real ~87
# words/paragraph average with a small buffer: 17 * 87 ~= 1480 (above the
# word floor), 24 * 87 ~= 2090 (under the word ceiling).
LF_MIN_PARAGRAPHS = 17
LF_MAX_PARAGRAPHS = 24

LF_MAX_SCRIPT_ATTEMPTS = 3
LF_QUALITY_THRESHOLD = QUALITY_THRESHOLD  # same bar as Shorts (8/10) for now

PARAGRAPH_GAP_MS = 550        # a bigger beat than Shorts' 220ms SENTENCE_GAP_MS -
                               # a paragraph break should read as a scene change

LF_MAX_CLIPS_PER_PARAGRAPH = 4  # caps Pexels calls/video - see module docstring
LF_MIN_CLIP_SLICE_SEC = 3.5     # never cut a B-roll slice shorter than this
LF_CAPTION_WORDS_PER_CHUNK = 3  # modern word-chunk captions (2026-07-21 restyle)

LF_MIN_VIDEO_DURATION_SEC = 480   # 8 minutes
LF_MAX_VIDEO_DURATION_SEC = 920   # ~15.3 minutes (small margin over 15:00)
LF_REQUIRED_WIDTH = LF_VIDEO_WIDTH
LF_REQUIRED_HEIGHT = LF_VIDEO_HEIGHT

LF_VIDEOS_SHEET_TAB = "LongformVideos!A:N"
LF_CHECKLIST_SHEET_TAB = "LongformChecklist!A:O"

# Phase 4 polish pass (2026-07-20): a beat longer than Shorts' 1.1s title
# card, proportionate to an 8+ minute video instead of a 60s Short.
LF_TITLE_CARD_SECONDS = 2.2

FORBIDDEN_HOOK_OPENERS = [
    "welcome back", "today we will discuss", "did you know",
    "in this video", "hey guys", "hey everyone", "what's up guys",
]

GENERIC_PHRASES = [
    "did you know that", "in this video we will", "welcome back to my channel",
    "smash that like button", "don't forget to subscribe",
    "today we will discuss", "in today's video", "stay tuned to find out",
]

# ---------------------------------------------------------------------------
# Script generation (Groq) - paragraph-structured, not sentence-structured
# ---------------------------------------------------------------------------

def generate_longform_script(topic: str, pillar: str, feedback: str = "") -> dict:
    """Generate a long-form (8-15 min) documentary-style script as a list of
    PARAGRAPHS rather than individual sentences (see module docstring for
    why - this drives paragraph-level TTS/B-roll chunking downstream).

    Rewritten 2026-07-20 (Phase 4 polish pass) after the first real upload
    ("Why Embarrassing Moments Last") was watched end to end and critiqued:
    the script cleared every quality/word-count gate but still read as a
    flat explainer essay rather than a story - it never built tension
    paragraph to paragraph, and the closing paragraph was a generic
    "in conclusion, X is a multifaceted phenomenon influenced by a range of
    psychological, social, and emotional factors" sentence that didn't
    resolve anything or call back to the opening hook. This version adds an
    explicit STORY ARC requirement and a much stricter closing-paragraph
    spec, plus tighter visual_keywords guidance after the same review found
    several B-roll mismatches (a crying-emoji cartoon, an abstract
    kaleidoscope, a yoga class) that read as loose keyword matches rather
    than deliberate choices.
    """
    feedback_block = ""
    if feedback:
        feedback_block = textwrap.dedent(f"""

        IMPORTANT - a previous draft on this exact topic was reviewed and
        scored too low or was the wrong length because: "{feedback}"
        Write a genuinely different draft that specifically fixes that
        weakness, while still following every requirement below.
        """)

    tone = CONTENT_PILLARS[pillar]["tone"]
    prompt = textwrap.dedent(f"""
    You are the writer for MindByte, a YouTube channel about why humans
    think, feel and behave the way they do. This is a LONG-FORM video
    (8-15 minutes), not a Short - it should feel like a psychology
    documentary essay, positioned as a documentary essay channel - NOT a
    generic facts channel, listicle, or low-effort AI content farm.

    STORY ARC - this is the most important structural requirement. Do not
    write a list of separate facts about the topic. Write ONE continuous
    story with a real shape:
    1. An OPENING paragraph (the hook) that raises a specific, concrete
       question or tension the rest of the video will resolve.
    2. Several BODY paragraphs that ESCALATE - each one should go deeper
       or raise the stakes/curiosity higher than the one before it (a
       surprising mechanism, then a sharper example, then a twist or a
       counter-intuitive angle), building toward a turning point roughly
       three-quarters of the way through, rather than presenting
       disconnected facts in any order. Vary the angle paragraph to
       paragraph so no two paragraphs just restate the same point.
       IMPORTANT: the "turning point" is a twist or reveal in the IDEAS,
       not a wrap-up - no BODY paragraph may mention the channel name,
       subscribing, following, or invite the viewer to "join this
       journey"; that language is reserved for the single true final
       paragraph only (rule 3 below). A body paragraph that reads like a
       conclusion or a subscribe-style call to action is wrong even if it
       lands near the three-quarters mark.
    3. A CLOSING paragraph that resolves the opening hook - it must
       explicitly call back to the specific question or tension raised in
       paragraph 1 and land on ONE concrete, memorable final image, line,
       or insight the viewer will remember. This is a hard requirement:
       do NOT end on a generic academic-summary sentence. Never use
       phrasing like "multifaceted phenomenon", "influenced by a range
       of", "in conclusion", "to sum up", "in summary", or any sentence
       that could be pasted onto a completely different topic without
       changing the meaning - if the closing sentence would still make
       sense with the topic swapped out, it is too generic and must be
       rewritten. Include a brief, natural mention of the channel name
       "MindByte" inviting the viewer to keep watching/subscribe if the
       channel resonates with them - phrased originally, never a generic
       "smash that like button" / "don't forget to subscribe" line.
       This is the ONLY paragraph allowed to mention the channel name,
       subscribing, or following - if any earlier paragraph does this
       too, the script is wrong and must be rewritten before returning
       it.

    HOOK RULES - the opening paragraph's first sentence decides whether
    anyone stays: never start with "Welcome back", "Today we will
    discuss", "Did you know", or any greeting/announcement. Open with a
    surprising statement, a psychological question, or an emotional
    trigger, in your own original words.

    Requirements:
    - ORIGINAL wording throughout - your own framing, not copied phrasing
      from any single source.
    - No clinical, diagnostic, or medical advice - general-interest
      psychology, not therapy or a diagnosis.
    - HARD REQUIREMENT: produce at LEAST {LF_MIN_PARAGRAPHS} paragraphs, and
      up to {LF_MAX_PARAGRAPHS}. This is a real constraint, not a
      suggestion - a script with fewer than {LF_MIN_PARAGRAPHS} paragraphs
      is a failed response no matter how good each paragraph is. Err
      toward MORE, shorter body paragraphs (each a real, developed
      paragraph of roughly 80-150 words - not a one-liner) covering more
      distinct angles, rather than fewer, longer ones.
    - HARD REQUIREMENT: total narration across all paragraphs combined
      must be between {LF_MIN_WORDS} and {LF_MAX_WORDS} words. Count
      before finalizing - under {LF_MIN_WORDS} words is a failed response.
      If you're unsure whether you've written enough, add another body
      paragraph exploring a fresh angle rather than stopping early.
    - Keep a documentary pace: confident, and increasingly tense or
      suspenseful as the story builds toward its turning point - never
      robotic, never list-like, and never the same flat energy from the
      first paragraph to the last. The throughline must read as ONE
      coherent story with rising stakes, not disconnected facts stitched
      together.
    - Choose genuinely interesting angles and, where natural, reference
      real, well-known psychological concepts or classic findings (in
      your own words - do not quote any source verbatim).
    - Also produce a clickable title (under 90 characters) that creates
      curiosity without being clickbait-false.
    - Also produce a YouTube description (3-5 sentences plus 4-6 relevant
      hashtags).
    - Also produce "tags": an array of 12-18 SEARCH TERMS a real viewer
      would type into YouTube (a mix of broad terms like "psychology",
      "human behavior", "why people", terms specific to "{topic}", and
      the channel name "MindByte").
    - Each paragraph object must include "visual_keywords": an array of
      3-4 short (2-3 word) stock-video search phrases for REAL,
      human-centric, LITERAL footage that matches what THAT paragraph is
      actually describing (a specific person doing a specific relatable
      action, a real setting, a real object) - never a cartoon, emoji,
      illustration, clip-art, kaleidoscope, or other abstract/symbolic
      motion-graphic query, and never a generic mood word on its own
      (like just "embarrassment" or "loop") that could return an
      unrelated abstract clip. If a paragraph is about a feeling or
      mechanism rather than a literal scene, describe a concrete everyday
      moment that captures it instead (e.g. for a paragraph about a
      mental loop, search for someone replaying a memory/pacing/lost in
      thought - not an abstract pattern).

    {tone}
    {feedback_block}

    Topic: {topic} (pillar: {pillar})

    Return ONLY valid JSON with this exact shape:
    {{
      "title": "...",
      "description": "...",
      "tags": ["...", "..."],
      "paragraphs": [
        {{"text": "...", "visual_keywords": ["...", "...", "..."]}}
      ]
    }}
    """).strip()

    raw = call_groq(prompt)
    data = json.loads(raw)

    paragraphs = data.get("paragraphs", [])
    # Defensive: guarantee every paragraph has at least one visual keyword
    # even if the model drifts from the requested shape, since B-roll
    # gathering depends on it.
    for p in paragraphs:
        if not p.get("visual_keywords"):
            p["visual_keywords"] = [data.get("title", "people talking")]
    data["paragraphs"] = paragraphs

    if not data.get("tags"):
        # Fall back to a flattened set of visual keywords rather than
        # crashing upload on a missing "tags" field.
        flat_kw = [kw for p in paragraphs for kw in p.get("visual_keywords", [])]
        data["tags"] = flat_kw[:15] if flat_kw else [topic, "MindByte", "psychology"]

    return data



def score_longform_quality(topic: str, pillar: str, script: dict) -> dict:
    full_text = " ".join(p["text"] for p in script["paragraphs"])
    word_count = sum(len(p["text"].split()) for p in script["paragraphs"])
    prompt = textwrap.dedent(f"""
    Rate this long-form (8-15 minute) YouTube script for MindByte, a
    psychology / human-behavior channel positioned as a documentary
    essay channel - NOT a generic facts channel, listicle, or low-effort
    AI content farm.

    Score primarily on:
    - Hook strength: does the opening paragraph grab attention
      immediately, without a generic opener?
    - Depth and structure: does it explore the topic from multiple real
      angles across the paragraphs, building rather than repeating
      itself, with a clear arc from question to insight?
    - Emotional pull: would a viewer think "that explains me" or "I never
      realized this"?
    - Originality: no generic filler like "did you know" or "stay tuned
      to find out", and the angles are not the most obvious 101-level
      trivia.
    - Pacing for the length: does {word_count} words of content feel
      earned and full, or padded/repetitive for a video this long?

    Only score below 6 if the script is genuinely boring, generic,
    repetitive, or reads like padded filler rather than a real essay.

    Topic: {topic} (pillar: {pillar})
    Title: {script['title']}
    Script: {full_text}

    Return ONLY valid JSON: {{"score": <integer 1-10>, "notes": "<one
    sentence justification>"}}
    """).strip()
    raw = call_groq(prompt)
    return json.loads(raw)


def expand_longform_script(script: dict, topic: str, pillar: str, words_needed: int) -> dict:
    """Last-resort top-up used when generate_and_score_longform_script()
    still comes in under LF_MIN_WORDS after LF_MAX_SCRIPT_ATTEMPTS full
    rerolls (run #1, 2026-07-19, hit exactly this - 3/3 attempts landed at
    784-877 words against a 1400 floor). Re-rolling the WHOLE script again
    already proved not to reliably fix a shortfall, so instead this asks
    Groq to write additional body paragraphs that continue the existing,
    already-scored piece, and splices them in before the closing paragraph
    - preserving the opening hook and closing takeaway that already earned
    their quality score, rather than gambling on a 4th full reroll.
    """
    tone = CONTENT_PILLARS[pillar]["tone"]
    existing_paragraphs = script["paragraphs"]
    # ~87 words/paragraph is the real observed average (see LF_MIN_PARAGRAPHS
    # comment above) - size the ask off that, with a minimum of 2 paragraphs
    # so a small shortfall still gets a meaningful topped-up angle. Capped so
    # the merged script can't blow past LF_MAX_PARAGRAPHS and trip the
    # pre-publish checklist's paragraph_count_ok check on the way to fixing
    # duration_ok.
    room_left = max(0, LF_MAX_PARAGRAPHS - len(existing_paragraphs))
    if room_left == 0:
        # Already at the paragraph ceiling - can't safely add any without
        # tripping paragraph_count_ok. Nothing more we can do here; return
        # the script unchanged rather than risk overshooting the cap.
        print(
            "[pipeline_longform] expansion skipped: already at "
            f"LF_MAX_PARAGRAPHS ({LF_MAX_PARAGRAPHS}) paragraphs"
        )
        return script
    extra_count = max(1, min(-(-words_needed // 87), room_left))
    existing_text = "\n\n".join(p["text"] for p in existing_paragraphs)
    prompt = textwrap.dedent(f"""
    You are continuing a long-form YouTube documentary-essay script for
    MindByte (topic: "{topic}", pillar: "{pillar}", tone: {tone}). The
    script so far is:

    {existing_text}

    This script is currently too SHORT for an 8-15 minute video. Write
    exactly {extra_count} ADDITIONAL body paragraphs (roughly 80-150 words
    each, same documentary-essay tone) that explore genuinely NEW angles,
    mechanisms, or examples not already covered above - do not repeat any
    point already made. These will be inserted into the middle of the
    script, between the existing opening and closing paragraphs, so do not
    write a new hook or a new closing/subscribe line.

    Each paragraph object must include "visual_keywords": an array of 3-4
    short (2-3 word) stock-video search phrases for real, human-centric
    footage matching that paragraph.

    Return ONLY valid JSON: {{"paragraphs": [{{"text": "...", "visual_keywords": ["...", "..."]}}]}}
    """).strip()
    raw = call_groq(prompt)
    data = json.loads(raw)
    new_paragraphs = data.get("paragraphs", [])
    for p in new_paragraphs:
        if not p.get("visual_keywords"):
            p["visual_keywords"] = [script.get("title", "people talking")]

    # Insert before the closing paragraph so the quotable takeaway +
    # channel mention still lands last.
    merged_paragraphs = existing_paragraphs[:-1] + new_paragraphs + existing_paragraphs[-1:]
    expanded = dict(script)
    expanded["paragraphs"] = merged_paragraphs
    return expanded


def generate_and_score_longform_script(topic: str, pillar: str,
                                        max_attempts: int = LF_MAX_SCRIPT_ATTEMPTS) -> tuple:
    """Mirrors pipeline.py's generate_and_score_script(), but the retry
    decision also checks the long-form word-count band (LF_MIN_WORDS /
    LF_MAX_WORDS) instead of a single floor, since a long-form script can
    drift too long just as easily as too short.
    """
    best_script, best_quality, best_meets_bar, best_word_count = (
        None, {"score": -1, "notes": ""}, False, -1,
    )
    attempts = []
    feedback = ""
    for attempt in range(1, max_attempts + 1):
        script = generate_longform_script(topic, pillar, feedback=feedback)
        quality = score_longform_quality(topic, pillar, script)
        word_count = sum(len(p["text"].split()) for p in script["paragraphs"])
        in_band = LF_MIN_WORDS <= word_count <= LF_MAX_WORDS
        meets_bar = quality["score"] >= LF_QUALITY_THRESHOLD and in_band
        print(
            f"[pipeline_longform] attempt {attempt}/{max_attempts}: "
            f"quality score {quality['score']} - {quality['notes']} "
            f"(word count: {word_count}, paragraphs: {len(script['paragraphs'])})"
        )
        attempts.append(
            {"script": script, "quality": quality, "word_count": word_count, "meets_bar": meets_bar}
        )
        is_better = (
            (meets_bar and not best_meets_bar)
            or (meets_bar == best_meets_bar and quality["score"] > best_quality["score"])
            or (
                meets_bar == best_meets_bar
                and quality["score"] == best_quality["score"]
                and abs(word_count - LF_TARGET_WORDS) < abs(best_word_count - LF_TARGET_WORDS)
            )
        )
        if best_script is None or is_better:
            best_script, best_quality, best_meets_bar, best_word_count = (
                script, quality, meets_bar, word_count,
            )
        if meets_bar:
            break
        if quality["score"] >= LF_QUALITY_THRESHOLD:
            para_count = len(script["paragraphs"])
            if word_count < LF_MIN_WORDS:
                feedback = (
                    f"the script scored well but was only {word_count} words - too short "
                    f"for an 8-15 minute video. This time, write AT LEAST "
                    f"{LF_MIN_PARAGRAPHS} paragraphs (you wrote {para_count} "
                    f"last time - that is not enough), covering more distinct "
                    f"angles and examples, to reach at least {LF_MIN_WORDS} "
                    f"words total."
                )
            else:
                feedback = (
                    f"the script scored well but was {word_count} words - too "
                    f"long. Tighten it to under {LF_MAX_WORDS} words by "
                    f"cutting the most repetitive paragraph."
                )
        else:
            feedback = quality.get("notes", "")

    if not best_meets_bar and best_word_count < LF_MIN_WORDS:
        print(
            f"[pipeline_longform] still short after {max_attempts} full "
            f"attempts ({best_word_count} words) - topping up the best "
            f"draft with additional paragraphs instead of rerolling again"
        )
        try:
            expanded = expand_longform_script(
                best_script, topic, pillar, LF_MIN_WORDS - best_word_count,
            )
            expanded_word_count = sum(len(p["text"].split()) for p in expanded["paragraphs"])
            expanded_quality = score_longform_quality(topic, pillar, expanded)
            expanded_in_band = LF_MIN_WORDS <= expanded_word_count <= LF_MAX_WORDS
            print(
                f"[pipeline_longform] expanded draft: quality score "
                f"{expanded_quality['score']} - {expanded_quality['notes']} "
                f"(word count: {expanded_word_count}, paragraphs: "
                f"{len(expanded['paragraphs'])})"
            )
            expanded_meets_bar = expanded_in_band and expanded_quality["score"] >= LF_QUALITY_THRESHOLD
            attempts.append(
                {"script": expanded, "quality": expanded_quality,
                 "word_count": expanded_word_count, "meets_bar": expanded_meets_bar}
            )
            if expanded_meets_bar:
                best_script, best_quality = expanded, expanded_quality
            else:
                print(
                    "[pipeline_longform] expanded draft still didn't clear "
                    "the bar - continuing with the original best draft"
                )
        except Exception as e:  # noqa: BLE001 - expansion is a bonus, never abort the run
            print(f"[pipeline_longform] expansion attempt failed, continuing with best draft as-is: {e}")

        # None of the attempts (including the expansion, if it ran) cleared
        # the word-count floor. Previously this fell through to whichever
        # draft scored highest on quality - which could be the SHORTEST
        # draft generated all run, discarding a longer-but-slightly-lower-
        # quality candidate (like the expansion) that was actually closer to
        # a real 8+ minute video. Prefer word count among any candidate that
        # still clears the quality bar, since a too-short video fails the
        # pre-publish checklist outright while a slightly-lower (but still
        # acceptable) quality score does not.
        if not any(a["meets_bar"] for a in attempts):
            qualified = [a for a in attempts if a["quality"]["score"] >= LF_QUALITY_THRESHOLD]
            pool = qualified or attempts
            longest = max(pool, key=lambda a: a["word_count"])
            if longest["word_count"] > best_word_count:
                print(
                    f"[pipeline_longform] switching to the longest acceptable-quality "
                    f"draft ({longest['word_count']} words, quality "
                    f"{longest['quality']['score']}) instead of the highest-quality "
                    f"but shorter draft ({best_word_count} words, quality "
                    f"{best_quality['score']})"
                )
                best_script, best_quality, best_word_count = (
                    longest["script"], longest["quality"], longest["word_count"],
                )

    return best_script, best_quality

def compliance_check_longform(script: dict) -> dict:
    """Same lightweight originality/policy sanity check as pipeline.py's
    compliance_check(), adapted to the paragraph-based script shape."""
    lowered = " ".join(p["text"] for p in script["paragraphs"]).lower()
    flags = [p for p in GENERIC_PHRASES if p in lowered]
    passed = len(flags) == 0
    notes = "OK" if passed else f"Generic phrasing detected: {', '.join(flags)}"
    return {"passed": passed, "notes": notes}


# ---------------------------------------------------------------------------
# Pexels (landscape) - a separate function from pipeline.py's
# search_pexels_clip(), which hardcodes orientation="portrait"
# ---------------------------------------------------------------------------

def search_pexels_clip_longform(query: str, used_ids: set) -> dict | None:
    resp = SESSION.get(
        "https://api.pexels.com/videos/search",
        headers={"Authorization": PEXELS_API_KEY},
        params={"query": query, "orientation": "landscape", "per_page": 15},
        timeout=30,
    )
    if resp.status_code != 200:
        return None
    for video in resp.json().get("videos", []):
        if video["id"] in used_ids:
            continue
        files = sorted(
            video["video_files"],
            key=lambda f: (f.get("width") or 0) * (f.get("height") or 0),
            reverse=True,
        )
        for f in files:
            if f.get("width") and f.get("height") and f["width"] >= f["height"]:
                used_ids.add(video["id"])
                return {"id": video["id"], "url": f["link"]}
        if files:
            used_ids.add(video["id"])
            return {"id": video["id"], "url": files[0]["link"]}
    return None


def gather_clips_for_paragraph(keywords: list, workdir: str, paragraph_index: int,
                                used_ids: set) -> list:
    """Download up to LF_MAX_CLIPS_PER_PARAGRAPH clips for one paragraph's
    visual_keywords, sharing used_ids across the whole video so the same
    clip never repeats. Falls back through FALLBACK_QUERIES for any
    keyword slot that comes up empty, same pattern as pipeline.py's
    gather_clips(), so a paragraph is never left with zero B-roll.
    """
    clip_paths = []
    slots = keywords[:LF_MAX_CLIPS_PER_PARAGRAPH]
    for i, keyword in enumerate(slots):
        clip = search_pexels_clip_longform(keyword, used_ids)
        if not clip:
            for fb in FALLBACK_QUERIES:
                clip = search_pexels_clip_longform(fb, used_ids)
                if clip:
                    break
        if not clip:
            continue
        dest = os.path.join(workdir, f"p{paragraph_index}_clip_{i}.mp4")
        download_file(clip["url"], dest)
        clip_paths.append(dest)
    if not clip_paths:
        # Last resort: guarantee at least one clip per paragraph so
        # assembly never has to skip a whole segment of the video.
        # Updated 2026-07-20: collect up to LF_MAX_CLIPS_PER_PARAGRAPH
        # clips here instead of stopping at the first hit. A paragraph
        # that reaches this branch is usually a LATER paragraph in the
        # video - used_ids has grown large by then, so FALLBACK_QUERIES'
        # shared pool is more likely already tapped out for its first few
        # terms - and previously the first successful fallback would
        # `break` immediately, leaving assemble_video_longform() with
        # only one clip to stretch statically across the paragraph's
        # entire duration. That is what produced the long static B-roll
        # hold observed near the end of the first real long-form upload.
        for fb in FALLBACK_QUERIES:
            clip = search_pexels_clip_longform(fb, used_ids)
            if clip:
                fb_idx = len(clip_paths)
                dest = os.path.join(workdir, f"p{paragraph_index}_clip_fallback{fb_idx}.mp4")
                download_file(clip["url"], dest)
                clip_paths.append(dest)
            if len(clip_paths) >= LF_MAX_CLIPS_PER_PARAGRAPH:
                break
    return clip_paths


def gather_all_clips_longform(paragraphs: list, workdir: str) -> list:
    """Returns a list-of-lists: clip_groups[i] is the list of clip paths
    for paragraphs[i]. used_ids is shared across every paragraph in the
    video so B-roll never repeats a clip."""
    used_ids: set = set()
    clip_groups = []
    for i, para in enumerate(paragraphs):
        clips = gather_clips_for_paragraph(para["visual_keywords"], workdir, i, used_ids)
        clip_groups.append(clips)
    return clip_groups


# ---------------------------------------------------------------------------
# Voiceover - one edge-tts call per PARAGRAPH (not per sentence)
# ---------------------------------------------------------------------------

def generate_voiceover_paragraphs(paragraphs: list, workdir: str, pillar: str) -> tuple:
    """Synthesizes each paragraph as its own edge-tts clip (a paragraph is
    already a natural, multi-sentence unit for edge-tts - unlike Shorts'
    single-sentence clips, there's no flat-prosody problem to solve here
    since a paragraph-length Communicate() call has room to breathe), then
    splices them with a longer PARAGRAPH_GAP_MS silence than Shorts uses
    between sentences, so a paragraph break reads as a scene change.

    Returns (combined_audio_path, segment_durations) where
    segment_durations[i] is the real measured duration (seconds,
    including the trailing gap) of paragraphs[i]'s audio - used for both
    caption timing and dividing each paragraph's B-roll clips.
    """
    preset = CONTENT_PILLARS.get(pillar, {})
    voice = preset.get("voice", "en-US-AriaNeural")
    base_rate = preset.get("base_rate", 10)
    base_pitch = preset.get("base_pitch", 3)

    texts = [p["text"] for p in paragraphs]
    clip_paths = []
    for i, text in enumerate(texts):
        clip_path = os.path.join(workdir, f"voice_p{i}.mp3")
        # Reuse the same hook/closing/jitter prosody logic Shorts uses per
        # sentence, applied here per paragraph (index 0 = opening hook,
        # last index = closing takeaway).
        rate, pitch = _sentence_prosody(text, i, len(texts), base_rate, base_pitch)
        asyncio.run(_synthesize(text, clip_path, voice=voice, rate=rate, pitch=pitch))
        clip_paths.append(clip_path)

    gap_seconds = PARAGRAPH_GAP_MS / 1000
    segment_durations = [ffprobe_duration(p) + gap_seconds for p in clip_paths]

    silence_path = os.path.join(workdir, "silence_lf.mp3")
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
            "-t", f"{gap_seconds:.3f}", "-c:a", "libmp3lame", "-q:a", "4",
            silence_path,
        ],
        check=True, capture_output=True,
    )

    concat_list = os.path.join(workdir, "voice_concat_lf.txt")
    with open(concat_list, "w") as f:
        for i, p in enumerate(clip_paths):
            f.write(f"file '{os.path.abspath(p)}'\n")
            if i < len(clip_paths) - 1:
                f.write(f"file '{os.path.abspath(silence_path)}'\n")

    combined_path = os.path.join(workdir, "voiceover_lf.mp3")
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list,
            "-c:a", "libmp3lame", "-q:a", "4", combined_path,
        ],
        check=True, capture_output=True,
    )
    return combined_path, segment_durations


# ---------------------------------------------------------------------------
# Captions - landscape positioning, sentence-level chunks estimated within
# each paragraph's known real duration
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

def _split_paragraph_into_lines(text: str) -> list:
    parts = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text.strip()) if s.strip()]
    return parts or [text.strip()]


def build_ass_longform(paragraphs: list, segment_durations: list, dest_path: str) -> None:
    """Builds a landscape .ass caption file. Unlike Shorts (one Dialogue
    line per sentence, timed off a real per-sentence TTS clip), long-form
    TTS is synthesized per PARAGRAPH, so there's no real per-sentence
    timestamp. Instead each paragraph's real measured duration is split
    across its sentences proportionally to word count - a reasonable
    estimate for caption pacing, same principle pipeline.py used before it
    moved to real per-sentence timing for Shorts.

    Positioning is a conventional lower-third (MarginV=90) rather than
    Shorts' MarginV=420 - there's no mobile Shorts UI to avoid in a
    landscape long-form video.
    """
    def fmt(ts: float) -> str:
        cs = int(round(ts * 100))
        h, cs = divmod(cs, 360000)
        m, cs = divmod(cs, 6000)
        s, cs = divmod(cs, 100)
        return f"{h:01d}:{m:02d}:{s:02d}.{cs:02d}"

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {LF_VIDEO_WIDTH}\n"
        f"PlayResY: {LF_VIDEO_HEIGHT}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
                "Style: Caption,Arial,78,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,1,0,0,0,100,100,1,0,1,4,1,2,120,120,96,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = [header]
    t = 0.0
    for para, dur in zip(paragraphs, segment_durations):
        sentences = _split_paragraph_into_lines(para["text"])
        word_counts = [max(len(s.split()), 1) for s in sentences]
        total_words = sum(word_counts)
        p_start = t
        p_end = t + dur
        cursor = p_start
        for sentence, wc in zip(sentences, word_counts):
            share = dur * (wc / total_words)
            s_start, s_end = cursor, min(cursor + share, p_end)
            cursor = s_end
            words = sentence.strip().split() or [sentence.strip()]
            n = LF_CAPTION_WORDS_PER_CHUNK
            chunks = [words[k:k + n] for k in range(0, len(words), n)]
            c_cursor = s_start
            for chunk in chunks:
                c_share = (s_end - s_start) * (len(chunk) / len(words))
                c_start, c_end = c_cursor, min(c_cursor + c_share, s_end)
                c_cursor = c_end
                emphasis = chunk.index(max(chunk, key=len))
                parts = [("{\\c&H00FFFF&}" + w.upper() + "{\\c&HFFFFFF&}") if k2 == emphasis else w.upper() for k2, w in enumerate(chunk)]
                pop = "{\\fad(40,20)\\fscx82\\fscy82\\t(0,110,\\fscx100\\fscy100)}"
                lines.append(f"Dialogue: 0,{fmt(c_start)},{fmt(c_end)},Caption,,0,0,0,,{pop}{' '.join(parts)}")
        t = p_end
    with open(dest_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Video assembly - landscape, multiple clips per paragraph
# ---------------------------------------------------------------------------

def build_title_card_longform(dest_path: str, title: str, pillar: str) -> None:
    """Landscape (1920x1080) counterpart to pipeline.py's build_title_card()
    (which is sized for a portrait Short) - same brand gradient/logo/
    wordmark treatment, laid out for a wide frame instead of a tall one.
    Added in the Phase 4 polish pass (2026-07-20): the long-form pipeline's
    first real upload ("Why Embarrassing Moments Last") had zero opening
    branding at all, unlike every Shorts upload - this closes that gap."""
    width, height = LF_VIDEO_WIDTH, LF_VIDEO_HEIGHT
    img = Image.new("RGB", (width, height), BRAND_BG_TOP)
    draw = ImageDraw.Draw(img)
    for y in range(height):
        t = y / height
        r = int(BRAND_BG_TOP[0] + (BRAND_BG_BOTTOM[0] - BRAND_BG_TOP[0]) * t)
        g = int(BRAND_BG_TOP[1] + (BRAND_BG_BOTTOM[1] - BRAND_BG_TOP[1]) * t)
        b = int(BRAND_BG_TOP[2] + (BRAND_BG_BOTTOM[2] - BRAND_BG_TOP[2]) * t)
        draw.line([(0, y), (width, y)], fill=(r, g, b))

    accent = PILLAR_ACCENT_COLORS.get(pillar, (255, 255, 255))
    logo_cy = int(height * 0.26)
    logo_r = height * 0.11
    _draw_logo_mark(draw, width / 2, logo_cy, logo_r, color=(255, 255, 255))

    wordmark_font = _brand_font(int(height * 0.095))
    wm_bbox = draw.textbbox((0, 0), BRAND_NAME, font=wordmark_font)
    wm_w = wm_bbox[2] - wm_bbox[0]
    draw.text((width / 2 - wm_w / 2, logo_cy + logo_r + int(height * 0.03)), BRAND_NAME,
              font=wordmark_font, fill=BRAND_TEXT_COLOR)

    bar_y = int(height * 0.60)
    bar_w = int(width * 0.14)
    draw.rectangle([width / 2 - bar_w / 2, bar_y, width / 2 + bar_w / 2, bar_y + 6], fill=accent)

    title_font = _brand_font(int(height * 0.075))
    words = title.split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textbbox((0, 0), trial, font=title_font)[2] > width * 0.72 and cur:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    if cur:
        lines.append(cur)
    lines = lines[:2]
    line_h = int(height * 0.10)
    ty = bar_y + 30
    for line in lines:
        lb = draw.textbbox((0, 0), line, font=title_font)
        lw = lb[2] - lb[0]
        draw.text((width / 2 - lw / 2, ty), line, font=title_font, fill=(255, 255, 255))
        ty += line_h

    img.save(dest_path)


def build_thumbnail_longform(dest_path: str, title: str, pillar: str) -> None:
    """Landscape (1920x1080) counterpart to pipeline.py's build_thumbnail().
    Long-form had NO custom thumbnail at all before this pass - YouTube was
    auto-picking a random mid-video frame with illegible baked-in caption
    text as the cover image, the single biggest gap flagged when the first
    real long-form upload was reviewed."""
    width, height = LF_VIDEO_WIDTH, LF_VIDEO_HEIGHT
    img = Image.new("RGB", (width, height), BRAND_BG_TOP)
    draw = ImageDraw.Draw(img)
    for y in range(height):
        f = y / height
        r = int(BRAND_BG_TOP[0] + (BRAND_BG_BOTTOM[0] - BRAND_BG_TOP[0]) * f)
        g = int(BRAND_BG_TOP[1] + (BRAND_BG_BOTTOM[1] - BRAND_BG_TOP[1]) * f)
        b = int(BRAND_BG_TOP[2] + (BRAND_BG_BOTTOM[2] - BRAND_BG_TOP[2]) * f)
        draw.line([(0, y), (width, y)], fill=(r, g, b))

    accent = PILLAR_ACCENT_COLORS.get(pillar, (255, 255, 255))

    title_font = _brand_font(int(height * 0.16))
    words = title.split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textbbox((0, 0), trial, font=title_font)[2] > width * 0.86 and cur:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    if cur:
        lines.append(cur)
    lines = lines[:3]

    line_h = int(height * 0.185)
    block_h = line_h * len(lines)
    ty = int(height * 0.42) - block_h // 2
    for line in lines:
        lb = draw.textbbox((0, 0), line, font=title_font)
        lw = lb[2] - lb[0]
        draw.text((width / 2 - lw / 2, ty), line, font=title_font, fill=(255, 255, 255))
        ty += line_h

    bar_y = ty + 14
    bar_w = int(width * 0.22)
    draw.rectangle([width / 2 - bar_w / 2, bar_y, width / 2 + bar_w / 2, bar_y + 8], fill=accent)

    ax = int(width * 0.5)
    ay = bar_y + int(height * 0.05)
    arrow_size = int(width * 0.028)
    draw.polygon(
        [
            (ax - arrow_size, ay),
            (ax + arrow_size, ay),
            (ax, ay + int(arrow_size * 1.3)),
        ],
        fill=accent,
    )

    logo_cy = int(height * 0.90)
    logo_cx = int(width * 0.42)
    logo_r = width * 0.02
    _draw_logo_mark(draw, logo_cx, logo_cy, logo_r, color=(255, 255, 255))
    small_font = _brand_font(int(height * 0.05))
    draw.text((logo_cx + logo_r + 14, logo_cy - int(height * 0.028)), BRAND_NAME,
              font=small_font, fill=BRAND_TEXT_COLOR)

    img.save(dest_path)


def assemble_video_longform(clip_groups: list, segment_durations: list, audio_path: str,
                             ass_path: str, output_path: str,
                             title_card_path: str = None, watermark_path: str = None) -> None:
    """Each paragraph's real measured audio duration is divided evenly
    across that paragraph's B-roll clips (clip_groups[i]), so a paragraph
    with 3 clips gets 3 roughly-equal slices covering its exact duration,
    instead of one clip stretched or looped awkwardly to fill a much
    longer paragraph than a single Pexels clip typically runs.

    Updated 2026-07-20 (Phase 4 polish pass): accepts an optional branded
    title_card_path/watermark_path (composited via ffmpeg overlay, same
    technique as pipeline.py's assemble_video() - verified locally first
    with a synthetic render before this went live: the title card image is
    fed in as a short, non-looped -t-bounded input so overlay(eof_action=
    pass) stops applying it once its own duration ends, and the watermark
    image is looped for the whole output so it persists throughout; total
    output duration is unaffected either way, confirmed via ffprobe on the
    test render). Also varies the per-paragraph B-roll cut rate (shorter,
    more frequent slices in the back half of the video than the front
    half) - a direct response to user feedback that a full episode felt
    monotone at one flat pace throughout.
    """
    workdir = os.path.dirname(output_path)
    normalized = []
    idx = 0
    last_good_clip = None
    total_paragraphs = len(clip_groups)
    for para_idx, (clips, dur) in enumerate(zip(clip_groups, segment_durations)):
        if not clips:
            # A paragraph with zero B-roll clips used to be silently
            # dropped from the video track here, while its narration
            # stayed in the full audio track - the final ffmpeg merge
            # uses -shortest, so a dropped paragraph shortened the whole
            # video (this is the diagnosed cause of run #3's duration_ok
            # failure despite the script clearing the word-count floor).
            # Reuse the previous paragraph's clip instead so this
            # paragraph's runtime is still represented on the video track.
            if last_good_clip is None:
                print("[pipeline_longform] warning: a paragraph has zero B-roll clips and there is no earlier clip yet to reuse - its runtime will be missing from the final video")
                continue
            print("[pipeline_longform] warning: a paragraph has zero B-roll clips - reusing the previous paragraph's clip so its runtime isn't dropped")
            clips = [last_good_clip]
        n = len(clips)
        # Pacing variation: bias toward more, shorter cuts in the back half
        # of the video than the front half, instead of one flat, even
        # rhythm for the whole runtime - front-loaded paragraphs (setup)
        # get slightly longer holds, later paragraphs (rising tension
        # toward the turning point) get cut faster.
        position = para_idx / max(1, total_paragraphs - 1)
        slice_floor = LF_MIN_CLIP_SLICE_SEC * (1.25 - 0.5 * position)
        slice_floor = max(2.2, slice_floor)
        # Never cut a slice shorter than slice_floor - if the even split
        # would go below that floor, reduce how many of this paragraph's
        # clips actually get used instead.
        max_slices = max(1, int(dur // slice_floor))
        n = min(n, max_slices)
        clips = clips[:n]
        slice_dur = dur / n
        for clip in clips:
            norm_path = os.path.join(workdir, f"norm_{idx}.mp4")
            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", clip, "-t", f"{slice_dur:.3f}",
                    "-vf",
                    f"scale={LF_VIDEO_WIDTH}:{LF_VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
                    f"crop={LF_VIDEO_WIDTH}:{LF_VIDEO_HEIGHT},fps=30,setsar=1",
                    "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                    "-pix_fmt", "yuv420p",
                    norm_path,
                ],
                check=True, capture_output=True,
            )
            normalized.append(norm_path)
            idx += 1
        last_good_clip = clips[-1]

    concat_list = os.path.join(workdir, "concat_lf.txt")
    with open(concat_list, "w") as f:
        for p in normalized:
            f.write(f"file '{os.path.abspath(p)}'\n")

    silent_video = os.path.join(workdir, "silent_lf.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list,
         "-c", "copy", silent_video],
        check=True, capture_output=True,
    )

    ass_escaped = ass_path.replace(":", r"\:")
    total_duration = sum(segment_durations)
    follow_from = max(total_duration - 3.0, 0.0)
    follow_overlay = (
        "drawtext=text='Subscribe to MindByte for more':fontcolor=white:fontsize=48:"
        "font=Arial:box=1:boxcolor=black@0.45:boxborderw=14:"
        f"x=(w-text_w)/2:y=60:enable='gte(t\\,{follow_from:.3f})'"
    )
    base_filter = f"subtitles={ass_escaped},{follow_overlay}"

    cmd = ["ffmpeg", "-y", "-i", silent_video, "-i", audio_path]
    if title_card_path and watermark_path:
        cmd += ["-loop", "1", "-t", f"{LF_TITLE_CARD_SECONDS}", "-i", title_card_path,
                "-loop", "1", "-i", watermark_path]
        filter_complex = (
            f"[0:v]{base_filter}[capped];"
            "[capped][2:v]overlay=eof_action=pass[withtitle];"
            "[withtitle][3:v]overlay=x=40:y=40[outv]"
        )
        cmd += ["-filter_complex", filter_complex, "-map", "[outv]", "-map", "1:a:0"]
    elif title_card_path:
        cmd += ["-loop", "1", "-t", f"{LF_TITLE_CARD_SECONDS}", "-i", title_card_path]
        filter_complex = (
            f"[0:v]{base_filter}[capped];"
            "[capped][2:v]overlay=eof_action=pass[outv]"
        )
        cmd += ["-filter_complex", filter_complex, "-map", "[outv]", "-map", "1:a:0"]
    elif watermark_path:
        cmd += ["-loop", "1", "-i", watermark_path]
        filter_complex = (
            f"[0:v]{base_filter}[capped];"
            "[capped][2:v]overlay=x=40:y=40[outv]"
        )
        cmd += ["-filter_complex", filter_complex, "-map", "[outv]", "-map", "1:a:0"]
    else:
        cmd += ["-vf", base_filter, "-map", "0:v:0", "-map", "1:a:0"]

    cmd += [
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", "192k", "-shortest",
        output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


# ---------------------------------------------------------------------------
# Pre-publish checklist (long-form thresholds) + Sheet logging
# ---------------------------------------------------------------------------

def run_prepublish_checklist_longform(
    topic: str, pillar: str, script: dict, quality: dict, compliance: dict,
    idea_score_avg: float, video_path: str,
) -> dict:
    checks = {}

    paragraphs = script.get("paragraphs", [])
    hook = paragraphs[0]["text"].strip().lower() if paragraphs else ""
    checks["hook_ok"] = not any(hook.startswith(p) for p in FORBIDDEN_HOOK_OPENERS)
    checks["idea_score_ok"] = idea_score_avg >= IDEA_SCORE_AVG_THRESHOLD
    checks["script_quality_ok"] = quality["score"] >= LF_QUALITY_THRESHOLD
    checks["compliance_ok"] = compliance["passed"]
    checks["tags_ok"] = 5 <= len(script.get("tags", [])) <= 25
    checks["paragraph_count_ok"] = LF_MIN_PARAGRAPHS <= len(paragraphs) <= LF_MAX_PARAGRAPHS

    try:
        info = ffprobe_video_info(video_path)
    except Exception as e:  # noqa: BLE001 - never crash the checklist itself
        info = {"duration": 0, "width": 0, "height": 0, "has_audio": False}
        print(f"[pipeline_longform] checklist: ffprobe failed, treating as fail: {e}")

    checks["duration_ok"] = LF_MIN_VIDEO_DURATION_SEC <= info["duration"] <= LF_MAX_VIDEO_DURATION_SEC
    checks["resolution_ok"] = info["width"] == LF_REQUIRED_WIDTH and info["height"] == LF_REQUIRED_HEIGHT
    checks["audio_ok"] = info["has_audio"]

    failed = [name for name, ok in checks.items() if not ok]
    return {
        "checks": checks,
        "failed": failed,
        "overall_pass": len(failed) == 0,
        "duration": info["duration"],
        "width": info["width"],
        "height": info["height"],
    }


def log_longform_checklist(access_token: str, topic: str, pillar: str, result: dict,
                            video_id: str = "") -> None:
    """Writes one row per long-form video to the 'LongformChecklist' Sheet
    tab, mirroring pipeline.py's log_quality_checklist(). Wrapped in
    try/except since this new tab won't exist in the Sheet until the
    channel owner creates it - a missing tab must never abort the run
    (same lesson learned from the Shorts QualityChecklist tab, commit
    da9c914)."""
    checks = result["checks"]
    row = [
        datetime.now(timezone.utc).isoformat(),
        video_id,
        topic,
        pillar,
        "PASS" if result["overall_pass"] else "FAIL",
        "Y" if checks.get("hook_ok") else "N",
        "Y" if checks.get("idea_score_ok") else "N",
        "Y" if checks.get("script_quality_ok") else "N",
        "Y" if checks.get("compliance_ok") else "N",
        "Y" if checks.get("duration_ok") else "N",
        "Y" if checks.get("resolution_ok") else "N",
        "Y" if checks.get("audio_ok") else "N",
        "Y" if checks.get("tags_ok") else "N",
        "Y" if checks.get("paragraph_count_ok") else "N",
        ", ".join(result["failed"]) if result["failed"] else "",
    ]
    try:
        sheet_append(access_token, LF_CHECKLIST_SHEET_TAB, row)
    except Exception as e:  # noqa: BLE001 - logging must never abort the run
        print(
            f"[pipeline_longform] could not log to LongformChecklist tab "
            f"(does it exist in the Sheet yet?): {e}"
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    access_token = get_access_token()
    topic, pillar, idea_score_avg = pick_topic_with_idea_score(access_token)
    print(f"[pipeline_longform] topic: {topic} (pillar: {pillar}) - idea score avg {idea_score_avg:.1f}")

    script, quality = generate_and_score_longform_script(topic, pillar)
    word_count = sum(len(p["text"].split()) for p in script["paragraphs"])
    print(f"[pipeline_longform] title: {script['title']}")
    print(
        f"[pipeline_longform] final quality score: {quality['score']} - "
        f"{quality['notes']} (word count: {word_count}, "
        f"paragraphs: {len(script['paragraphs'])})"
    )

    compliance = compliance_check_longform(script)
    print(f"[pipeline_longform] compliance: {compliance}")

    created_date = datetime.now(timezone.utc).isoformat()
    # LongformVideos columns: video_id, title, topic, status, created_date,
    # publish_at, quality_score, compliance_notes, duration_sec,
    # paragraph_count, word_count, idea_score_avg, url, notes  (A:N)
    sheet_row_base = [
        "", script["title"], topic, "", created_date, "",
        quality["score"], compliance["notes"], 0,
        len(script["paragraphs"]), word_count, round(idea_score_avg, 1), "", "",
    ]

    def log_video_row():
        try:
            sheet_append(access_token, LF_VIDEOS_SHEET_TAB, sheet_row_base)
        except Exception as e:  # noqa: BLE001 - new tab may not exist yet
            print(
                f"[pipeline_longform] could not log to LongformVideos tab "
                f"(does it exist in the Sheet yet?): {e}"
            )

    if quality["score"] < LF_QUALITY_THRESHOLD or not compliance["passed"]:
        sheet_row_base[3] = "Rejected"
        sheet_row_base[13] = "Skipped upload: failed quality/compliance gate"
        log_video_row()
        print("[pipeline_longform] rejected by quality/compliance gate - no upload")
        return

    with tempfile.TemporaryDirectory() as workdir:
        clip_groups = gather_all_clips_longform(script["paragraphs"], workdir)
        if not any(clip_groups):
            sheet_row_base[3] = "Failed"
            sheet_row_base[13] = "No usable Pexels clips found"
            log_video_row()
            print("[pipeline_longform] no clips found - aborting")
            return

        audio_path, segment_durations = generate_voiceover_paragraphs(
            script["paragraphs"], workdir, pillar,
        )
        audio_duration = ffprobe_duration(audio_path)

        final_audio_path = audio_path
        description = script["description"]
        music_path = os.path.join(workdir, "music_lf.mp3")
        music_meta = fetch_background_music(music_path, pillar)
        if music_meta:
            mixed_path = os.path.join(workdir, "voiceover_lf_mixed.mp3")
            try:
                mix_background_music(audio_path, music_path, audio_duration, mixed_path)
                final_audio_path = mixed_path
                print(
                    f"[pipeline_longform] music: '{music_meta['title']}' by "
                    f"{music_meta['creator']} ({music_meta['license']})"
                )
                if music_meta["license"] != "cc0":
                    description += (
                        f"\n\nMusic: \"{music_meta['title']}\" by "
                        f"{music_meta['creator']} ({music_meta['license'].upper()})"
                    )
            except Exception as e:  # noqa: BLE001 - music mix must never abort the run
                print(f"[pipeline_longform] music mix failed, continuing without music: {e}")

        ass_path = os.path.join(workdir, "captions_lf.ass")
        build_ass_longform(script["paragraphs"], segment_durations, ass_path)

        # Phase 4 polish pass (2026-07-20): branded title card + persistent
        # watermark, ported from the Shorts pipeline - the first real
        # long-form upload had neither. Best-effort: any failure here
        # must never abort the run, same pattern as pipeline.py's main().
        title_card_path = None
        watermark_path = None
        try:
            title_card_path = os.path.join(workdir, "title_card_lf.png")
            build_title_card_longform(title_card_path, script["title"], pillar)
            watermark_path = os.path.join(workdir, "watermark_lf.png")
            build_watermark_png(watermark_path)
        except Exception as e:  # noqa: BLE001 - branding is a bonus, never abort the run
            print(f"[pipeline_longform] branding overlay generation failed, continuing without it: {e}")
            title_card_path = None
            watermark_path = None

        output_path = os.path.join(workdir, "final_lf.mp4")
        assemble_video_longform(
            clip_groups, segment_durations, final_audio_path, ass_path, output_path,
            title_card_path=title_card_path, watermark_path=watermark_path,
        )

        checklist = run_prepublish_checklist_longform(
            topic, pillar, script, quality, compliance, idea_score_avg, output_path,
        )
        print(f"[pipeline_longform] pre-publish checklist: {checklist['checks']} (measured duration={checklist['duration']:.1f}s, target range={LF_MIN_VIDEO_DURATION_SEC}-{LF_MAX_VIDEO_DURATION_SEC}s, width={checklist['width']}, height={checklist['height']})")
        sheet_row_base[8] = round(checklist["duration"], 1)
        if not checklist["overall_pass"]:
            sheet_row_base[3] = "Failed"
            sheet_row_base[13] = f"Failed pre-publish checklist: {', '.join(checklist['failed'])}"
            log_video_row()
            log_longform_checklist(access_token, topic, pillar, checklist)
            print(f"[pipeline_longform] rejected by pre-publish checklist: {checklist['failed']}")
            return

        publish_at = datetime.now(timezone.utc) + timedelta(hours=PUBLISH_DELAY_HOURS)
        video_id = upload_to_youtube(
            access_token, output_path, script["title"], description,
            script["tags"], publish_at.isoformat(),
        )
        print(f"[pipeline_longform] uploaded video id: {video_id}")

        # Phase 4 polish pass (2026-07-20): custom branded thumbnail,
        # ported from the Shorts pipeline - long-form had none before this
        # and was relying on a random auto-picked video frame as its cover.
        try:
            thumb_path = os.path.join(workdir, "thumbnail_lf.png")
            build_thumbnail_longform(thumb_path, script["title"], pillar)
            set_youtube_thumbnail(access_token, video_id, thumb_path)
            print("[pipeline_longform] custom branded thumbnail set")
        except Exception as e:  # noqa: BLE001 - thumbnail is a bonus, never abort the run
            print(f"[pipeline_longform] custom thumbnail upload failed, continuing: {e}")

        sheet_row_base[0] = video_id
        sheet_row_base[3] = "Scheduled"
        sheet_row_base[5] = publish_at.isoformat()
        sheet_row_base[12] = f"https://youtu.be/{video_id}"
        log_video_row()
        log_longform_checklist(access_token, topic, pillar, checklist, video_id)
        mark_topic_used(access_token, topic, video_id)
        print("[pipeline_longform] done")


if __name__ == "__main__":
    main()
