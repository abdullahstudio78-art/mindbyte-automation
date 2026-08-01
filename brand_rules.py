"""
MindByte Automation - shared brand/tone/guardrail constants.

Consolidates rules that previously lived inline (and duplicated) in both
pipeline.py's generate_script() and pipeline_longform.py's
generate_longform_script() prompts, plus the pre-publish hook-opener check
in both files' compliance gates. This is a pure dedup/single-source-of-truth
refactor: the actual prompt wording each file sends to Groq is unchanged -
both files now just import these constants instead of hardcoding their own
copies, so tone rules can no longer silently drift between Shorts and
long-form.

Nothing here changes existing behavior on its own; it is only wired in by
pipeline.py / pipeline_longform.py importing from this module.
"""

# The channel's documentary-not-generic-facts-channel identity line, shared
# verbatim by both formats' prompts.
CHANNEL_IDENTITY_LINE = (
    "a psychology documentary crossed with a storytelling channel - NOT a "
    "generic facts channel, NOT a listicle, NOT a low-effort AI content farm."
)

# The six MindByte content pillars / niche list (per the original content
# strategy doc). Kept here as the single source of truth for anything that
# needs to reference "the niche list" (e.g. weekly_review prompts, future
# topic tooling) without re-typing it inline.
CHANNEL_NICHE_LIST = [
    "Human Psychology",
    "Human Behavior",
    "Relationship Psychology",
    "Emotional Intelligence",
    "Social Psychology",
    "Personal Growth through Psychology",
]

# Hook openers that instantly kill a video's watch-time - checked by the
# pre-publish compliance gate in both pipeline.py and pipeline_longform.py.
# Previously two separate hardcoded copies of this exact list; now one.
FORBIDDEN_HOOK_OPENERS = [
    "welcome back", "today we will discuss", "did you know",
    "in this video", "hey guys", "hey everyone", "what's up guys",
]

# Generic, low-effort filler phrases banned from scripts/closings in both
# formats. Previously duplicated between the two files; now one list.
GENERIC_PHRASES = [
    "did you know that", "in this video we will", "welcome back to my channel",
    "smash that like button", "don't forget to subscribe",
    "today we will discuss", "in today's video", "stay tuned to find out",
]

# ---------------------------------------------------------------------------
# Subscriber-conversion CTA system (2026-08-01)
# ---------------------------------------------------------------------------
# Shared by both pipeline.py (Shorts - appended as a short spoken closing
# sentence, separate from the story's own punchy insight line) and
# pipeline_longform.py (long-form - folded into the required closing-
# paragraph "mention MindByte/subscribe" instruction that already existed).
# Single source of truth so the two formats can't drift, and so
# weekly_review.py can reference the same style keys when it starts
# recommending a winning style once real subscriber-conversion data exists
# per style (see VideoMeta's CTAStyle/CTAText columns).
#
# Each style is a short instruction to the script-writing prompt, not a
# fixed sentence - the model still writes original wording every time, so
# two videos in the same style never sound identical. GENERIC_PHRASES above
# is still the hard ban list (checked against the CTA line too) so a lazy
# "don't forget to subscribe" / "smash that like button" can never slip
# through regardless of style.
CTA_STYLES = {
    "curiosity": {
        "label": "Curiosity",
        "instruction": (
            "Tease that there is more to discover on this exact subject, "
            "and that subscribing is how the viewer won't miss the next "
            "one - in the spirit of (do not copy verbatim, write your own): "
            "\"There are many more psychology secrets to discover. "
            "Subscribe so you don't miss the next one.\""
        ),
    },
    "series": {
        "label": "Series",
        "instruction": (
            "Frame this video as one entry in an ongoing series on this "
            "topic/pillar, and invite the viewer to subscribe for the next "
            "one - in the spirit of (do not copy verbatim, write your own): "
            "\"This is part of a series on how the mind works. Subscribe "
            "for the next psychology insight.\""
        ),
    },
    "value": {
        "label": "Value",
        "instruction": (
            "State plainly what kind of content the channel offers and "
            "invite anyone who enjoys that to subscribe - in the spirit of "
            "(do not copy verbatim, write your own): \"If you enjoy "
            "learning how the human mind works, subscribe for more "
            "psychology content.\""
        ),
    },
    "question": {
        "label": "Question",
        "instruction": (
            "Ask the viewer a short reflective question that connects back "
            "to the topic just explained, then invite them to subscribe "
            "for more like it - in the spirit of (do not copy verbatim, "
            "write your own): \"Have you ever noticed this in yourself? "
            "Subscribe, because we'll explain many more like this.\""
        ),
    },
}

CTA_STYLE_KEYS = list(CTA_STYLES.keys())


def pick_next_cta_style(recent_styles: list) -> str:
    """Rotation logic for requirement #4 ("rotate CTA styles automatically
    and avoid repeating the same wording"): given the CTA styles used on
    the last few published videos (most-recent last), pick a style that
    wasn't used on either of the last two videos if possible, so the exact
    same style never plays back-to-back or every-other-video. Falls back to
    a fully random style once every style has been seen recently (there's
    nothing better to avoid at that point), and to a uniform random choice
    if recent_styles is empty (first-ever run / no history yet).

    This is intentionally simple, stateless-except-for-the-Sheet-read logic
    rather than a weighted/learned model - see the module docstring above
    for why: there's no per-video subscriber-conversion signal to learn
    from yet. Once weekly_review.py has that signal, THIS function is the
    single place a smarter (performance-weighted) selection would plug in
    without changing any call site.
    """
    import random
    recent_tail = [s for s in (recent_styles or [])[-2:] if s in CTA_STYLES]
    candidates = [k for k in CTA_STYLE_KEYS if k not in recent_tail]
    if not candidates:
        candidates = CTA_STYLE_KEYS
    return random.choice(candidates)
