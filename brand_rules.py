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
