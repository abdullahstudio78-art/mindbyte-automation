"""
MindByte Automation - main content pipeline.

Runs end-to-end: pick a topic, generate a script with Groq (Llama 3.3 70B),
source B-roll from Pexels, synthesize a voiceover, assemble the video with
ffmpeg, score quality, run a compliance check, upload/schedule to YouTube,
and log everything to the Google Sheet dashboard.

All secrets are read from environment variables (populated from GitHub
Actions Secrets in CI).
"""

import os
import io
import json
import random
import re
import subprocess
import tempfile
import textwrap
import time
from datetime import datetime, timedelta, timezone

import requests
import math
from PIL import Image, ImageDraw, ImageFont

# --- Character asset system (new) ---
# Additive illustrated-character B-roll supplement to the Pexels flow
# below. See character_assets.py for details. Never removes/disables the
# Pexels path - gather_clips() tries a character asset first per slot and
# falls back to search_pexels_clip() exactly as before if none is found.
from stock_sources import search_multi_source_clip
from character_assets import (
    render_environment_motion_clip,
    apply_atmosphere_overlay,
    load_characters_manifest,
    select_character_asset,
)
CHARACTERS_MANIFEST = load_characters_manifest()
# --- end character asset system (new) ---

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
PEXELS_API_KEY = os.environ["PEXELS_API_KEY"]
OAUTH_CLIENT_ID = os.environ["OAUTH_CLIENT_ID"]
OAUTH_CLIENT_SECRET = os.environ["OAUTH_CLIENT_SECRET"]
OAUTH_REFRESH_TOKEN = os.environ["OAUTH_REFRESH_TOKEN"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]

GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

PUBLISH_DELAY_HOURS = 18  # rolling safety delay before a video goes public
VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920

QUALITY_THRESHOLD = 8  # gate: script must score >= this to be produced/uploaded
MAX_SCRIPT_ATTEMPTS = 3  # in-run retries with feedback before giving up

# Content pillars for MindByte's positioning as a psychology documentary /
# human-behavior storytelling channel (not a generic facts/listicle
# channel) - each pillar carries a short "tone" description used to steer
# generate_script()'s voice per topic, plus its own topic list. TOPIC_POOL
# below is derived from this as a flat (topic, pillar_name) list so the
# rest of the pipeline (pick_topic, mark_topic_used, sheet logging) keeps
# working against a simple flat pool.
CONTENT_PILLARS = {
    "Relationship Psychology": {
        "tone": "warm, emotionally intimate, a little vulnerable",
        "voice": "en-US-JennyNeural",
        "base_rate": 5,
        "base_pitch": 6,
        "music_queries": [
            "emotional piano", "soft cinematic piano",
            "melancholy piano ambient", "tender emotional strings",
        ],
        "topics": [
            "why people lose interest in relationships",
            "why someone becomes emotionally distant",
            "the psychology of attraction",
            "attachment styles and how they form",
            "why people chase those who are unavailable",
            "the psychology behind breakups",
            "how real emotional connection forms",
            "communication mistakes that quietly ruin relationships",
            "why you can't stop thinking about someone",
            "the psychology of love and rejection",
        ],
    },
    "Human Behavior Psychology": {
        "tone": "sharp, revealing, a little provocative",
        "voice": "en-US-AriaNeural",
        "base_rate": 10,
        "base_pitch": 3,
        "music_queries": [
            "mysterious ambient", "curious ambient piano",
            "dark ambient minimal", "cinematic tension calm",
        ],
        "topics": [
            "why people lie even when it's pointless",
            "why humans crave validation",
            "why we unconsciously copy other people",
            "how power quietly changes a person's behavior",
            "why overthinking happens",
            "why we procrastinate on things we actually care about",
            "why humans fear change",
            "why people act differently in groups",
            "the hidden psychology behind everyday habits",
            "why people make irrational decisions",
        ],
    },
    "Social Psychology": {
        "tone": "confident, observational, socially savvy",
        "voice": "en-US-GuyNeural",
        "base_rate": 12,
        "base_pitch": 2,
        "music_queries": [
            "confident cinematic ambient", "modern ambient upbeat",
            "sleek ambient electronic", "inspiring ambient",
        ],
        "topics": [
            "the psychology of first impressions",
            "what body language really reveals",
            "the psychology of confidence",
            "what actually makes someone charismatic",
            "how social status shapes behavior",
            "the psychology of influence",
            "the science of persuasion",
            "why people follow trends without realizing",
            "how humans judge others in seconds",
            "the psychology of group behavior",
        ],
    },
    "Brain & Neuroscience": {
        "tone": "clear, confident, science-grounded but accessible",
        "voice": "en-GB-RyanNeural",
        "base_rate": 8,
        "base_pitch": 0,
        "music_queries": [
            "futuristic ambient", "ambient technology",
            "sci-fi atmospheric ambient", "electronic ambient minimal",
        ],
        "topics": [
            "how dopamine actually drives motivation",
            "how habits form in the brain",
            "how emotions quietly distort memory",
            "the psychology of the fear response",
            "how the brain really makes decisions",
            "common brain biases that trick you daily",
            "why anxiety happens without real danger",
            "why we remember embarrassing moments forever",
            "how emotions secretly control decisions",
        ],
    },
    "Emotional Intelligence": {
        "tone": "calm, supportive, growth-oriented",
        "voice": "en-GB-SoniaNeural",
        "base_rate": 2,
        "base_pitch": 4,
        "music_queries": [
            "calm ambient piano", "warm ambient acoustic",
            "peaceful ambient reflective", "soft inspiring ambient",
        ],
        "topics": [
            "understanding your own emotions",
            "the psychology of self-control",
            "what emotional maturity actually looks like",
            "how to handle rejection in a healthy way",
            "the psychology of building real confidence",
            "how to read other people's emotions",
            "emotional skills that quietly improve relationships",
        ],
    },
    "Psychology Experiments & Stories": {
        "tone": "documentary, narrative, slightly suspenseful",
        # Swapped from en-US-DavisNeural (2026-07-23): run #33 crashed with
        # edge_tts.exceptions.NoAudioReceived the first time this pillar's
        # voice was ever actually exercised on a live run - one of the 4
        # previously-unvalidated pillar voices. Falling back to
        # en-US-AriaNeural, the one voice confirmed working across dozens
        # of prior runs, rather than gambling on another unvalidated name.
        "voice": "en-US-AriaNeural",
        "base_rate": -3,
        "base_pitch": -4,
        "music_queries": [
            "suspense ambient tension", "dark documentary ambient",
            "subtle tension cinematic", "mysterious cinematic score",
        ],
        "topics": [
            "the Milgram obedience experiment",
            "the Stanford prison experiment",
            "the bystander effect experiment",
            "famous social psychology experiments",
            "real psychological case studies",
            "what classic experiments reveal about human nature",
        ],
    },
}

TOPIC_POOL = [
    (topic, pillar)
    for pillar, data in CONTENT_PILLARS.items()
    for topic in data["topics"]
]

SESSION = requests.Session()


# ---------------------------------------------------------------------------
# Google OAuth
# ---------------------------------------------------------------------------

def get_access_token() -> str:
    resp = SESSION.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": OAUTH_CLIENT_ID,
            "client_secret": OAUTH_CLIENT_SECRET,
            "refresh_token": OAUTH_REFRESH_TOKEN,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"[pipeline] OAuth token refresh failed: {resp.status_code} {resp.text}")
    resp.raise_for_status()
    return resp.json()["access_token"]


def google_headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Google Sheets helpers
# ---------------------------------------------------------------------------

SHEETS_BASE = f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_SHEET_ID}"


def sheet_get(access_token: str, a1_range: str) -> list:
    resp = SESSION.get(
        f"{SHEETS_BASE}/values/{a1_range}",
        headers=google_headers(access_token),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("values", [])


def sheet_append(access_token: str, a1_range: str, row: list) -> None:
    resp = SESSION.post(
        f"{SHEETS_BASE}/values/{a1_range}:append",
        headers=google_headers(access_token),
        params={"valueInputOption": "USER_ENTERED"},
        json={"values": [row]},
        timeout=30,
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Topic selection
# ---------------------------------------------------------------------------

def pick_topic(access_token: str) -> tuple:
    """Return a (topic, pillar_name) pair not yet used, or a random one if
    every topic in the pool has been used at least once already."""
    rows = sheet_get(access_token, "UsedTopics!A2:A")
    used = {r[0].strip().lower() for r in rows if r}
    available = [(t, p) for t, p in TOPIC_POOL if t.lower() not in used]
    if available:
        return random.choice(available)
    # Every topic has been used at least once - recycle randomly rather than
    # stalling the channel forever.
    return random.choice(TOPIC_POOL)

def mark_topic_used(access_token: str, topic: str, video_id: str) -> None:
    sheet_append(
        access_token,
        "UsedTopics!A:C",
        [topic, datetime.now(timezone.utc).isoformat(), video_id],
    )

IDEA_SCORE_AVG_THRESHOLD = 7.0  # average across the 5 idea-appeal axes
IDEA_SCORE_MIN_AXIS = 5  # no single axis allowed to be a big weak spot
MAX_IDEA_ATTEMPTS = 5  # how many topics to try before settling for the best seen

def score_topic_idea(topic: str, pillar: str) -> dict:
    """Score a topic IDEA (before any script is written) on the five appeal
    axes from the content strategy - curiosity, emotional impact, global
    appeal, evergreen value, share potential - so a technically-fine but
    forgettable topic doesn't quietly turn into an average video. This is
    separate from score_quality(), which grades the finished script."""
    prompt = textwrap.dedent(f"""
        Rate this YouTube Short topic idea for MindByte, a channel about why
        humans think, feel and behave the way they do (psychology
        documentary / storytelling tone, not a generic facts channel).

        Topic: "{topic}" (pillar: {pillar})

        Score each on a 1-10 scale:
        - curiosity: would this genuinely make someone stop scrolling?
        - emotional_impact: does this touch something people actually feel?
        - global_appeal: does this land with a general global audience, not
          a narrow or culturally-specific one?
        - evergreen_value: will this still be relevant and interesting in
          years, not just this week?
        - share_potential: would someone send this to a friend and say
          "this is so me" or "I never realized this"?

        Return ONLY valid JSON: {{"curiosity": <1-10>, "emotional_impact":
        <1-10>, "global_appeal": <1-10>, "evergreen_value": <1-10>,
        "share_potential": <1-10>, "notes": "<one sentence justification>"}}
    """).strip()
    raw = call_groq(prompt)
    return json.loads(raw)

def pick_topic_with_idea_score(access_token: str, max_attempts: int = MAX_IDEA_ATTEMPTS) -> tuple:
    """Pick a topic and vet the IDEA itself against the five appeal axes
    before committing to writing a full script for it - retrying with a
    different topic if the pick scores weak, and falling back to the
    best-scoring one seen within the attempt budget rather than stalling
    the channel."""
    tried = set()
    best = None
    for attempt in range(1, max_attempts + 1):
        topic, pillar = pick_topic(access_token)
        if (topic, pillar) in tried and len(tried) < len(TOPIC_POOL):
            continue
        tried.add((topic, pillar))
        scores = score_topic_idea(topic, pillar)
        axis_keys = (
            "curiosity", "emotional_impact", "global_appeal",
            "evergreen_value", "share_potential",
        )
        axis_values = [scores.get(k, 0) for k in axis_keys]
        avg = sum(axis_values) / len(axis_values)
        passes = avg >= IDEA_SCORE_AVG_THRESHOLD and min(axis_values) >= IDEA_SCORE_MIN_AXIS
        print(
            f"[pipeline] idea attempt {attempt}/{max_attempts}: '{topic}' "
            f"({pillar}) - avg {avg:.1f} - {scores.get('notes', '')}"
        )
        if best is None or avg > best[2]:
            best = (topic, pillar, avg)
        if passes:
            return topic, pillar, avg
    print(
        f"[pipeline] no idea cleared the {IDEA_SCORE_AVG_THRESHOLD} bar in "
        f"{max_attempts} attempts - using best seen: '{best[0]}'"
    )
    return best[0], best[1], best[2]

def call_groq(prompt: str, _retries: int = 2) -> str:
    """Same Groq call as before, now with retry-with-backoff on a 429
    (added 2026-07-19 after long-form run #2: the free/on-demand tier's
    tokens-per-minute ceiling got hit late in a long-form run's call
    sequence, breaking the expansion top-up fallback right when it was
    needed most). Groq's 429 response reports how long to wait ("Please
    try again in Xs") - honor that (plus a small safety margin) instead
    of giving up immediately. Any other error status still raises
    right away, unchanged from before."""
    for attempt in range(_retries + 1):
        resp = SESSION.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.9,
                "response_format": {"type": "json_object"},
            },
            timeout=60,
        )
        if resp.status_code == 429 and attempt < _retries:
            wait_s = 5.0
            match = re.search(r"try again in ([\d.]+)s", resp.text)
            if match:
                wait_s = float(match.group(1)) + 1.0
            print(f"[pipeline] Groq rate-limited (429) - waiting {wait_s:.1f}s and retrying "
                  f"(attempt {attempt + 1}/{_retries})")
            time.sleep(wait_s)
            continue
        if resp.status_code != 200:
            print(f"[pipeline] Groq call failed: {resp.status_code} {resp.text}")
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Visual category classification (A/B/C plan, 2026-07-23 design discussion)
# ---------------------------------------------------------------------------
# The pipeline should not treat every beat the same way. Per the explicit
# spec: classify each spoken sentence into one of three visual categories so
# gather_clips() can route it appropriately, rather than always trying an
# illustrated character asset first and falling back to stock:
#   A - Stock Footage: everyday real-world environments (cities, streets,
#       parks, offices, cafes, homes, traffic, nature, lifestyle). Byte should
#       NOT be inserted here - these beats should stay pure stock footage.
#   B - Hybrid Scene: a cinematic real-world environment with Byte appearing
#       naturally in it. True compositing (matched lighting/shadow/color grade
#       so Byte doesn't look pasted on) needs a background-removal step on
#       Byte's assets that hasn't been built yet - see NOTE in gather_clips().
#       Until that lands, Category B beats fall back to the same illustrated-
#       character-cutout handling Category C uses, same as before this change.
#   C - Custom AI Scene: ONLY for environments/concepts that can't realistically
#       exist in stock libraries - dreams, memories, abstract psychology,
#       surreal mental worlds, symbolic visuals, impossible scenarios. Routes
#       to the illustrated Environments system (render_environment_motion_clip).
def classify_scene_categories(sentences: list) -> list:
    """Classify each sentence into "A", "B", "C", or None (classification
    failed/unavailable for that slot). Returns a list of None (same length
    as `sentences`) on ANY failure - the classifier is an ADDITIVE routing
    layer, not a required dependency, so a Groq outage or malformed response
    must never break a run. gather_clips() treats None the same as before
    this feature existed (character-first-then-stock, unchanged)."""
    if not sentences:
        return []
    numbered = "\n".join(f"{i}: {s}" for i, s in enumerate(sentences))
    prompt = (
        "Classify each numbered sentence below into exactly one visual category "
        "for a short psychology video. This is a REAL stock-footage-vs-custom-art "
        "routing decision, not a creative-writing exercise - default to A or B "
        "unless the sentence is IMPOSSIBLE to represent with a real filmed "
        "environment. Category C is expensive and should be RARE: in a typical "
        "16-sentence psychology script, expect roughly 8-12 sentences as A, "
        "3-6 as B, and at most 1-3 as C. If you're unsure between B and C, "
        "choose B. If you're unsure between A and B, choose A.\n\n"
        "A - Stock Footage (DEFAULT, most common): any everyday real-world "
        "environment or generic human activity that exists in real stock "
        "footage libraries - cities, streets, parks, offices, cafes, homes, "
        "bedrooms, traffic, nature, walking, typing, phones, conversations, "
        "commuting, exercising, cooking, etc. Most factual/explanatory/"
        "relatable statements about feelings, behavior, or relationships "
        "still belong here - a sentence describing an emotion does NOT by "
        "itself require anything other than a real-world clip of a person "
        "experiencing that emotion (e.g. someone looking hurt, someone alone "
        "at a window, two people talking) - it does not need our illustrated "
        "character or a custom drawn scene.\n"
        "B - Hybrid Scene: only when the sentence is specifically about OUR "
        "recurring narrator character (Byte) doing, feeling, or reacting to "
        "something in an ordinary real place (at a desk, walking a street, "
        "in a bedroom, at a cafe) - i.e. sentences that read as Byte's own "
        "direct experience or address to the viewer, not a general statement "
        "about people/emotions in the abstract.\n"
        "C - Custom AI Scene (RARE - use sparingly): reserve strictly for "
        "concepts that CANNOT be represented by any real filmed environment "
        "at all - literal dreams, literal memories being replayed, abstract "
        "mental/psychological metaphors made visual (a fractured mind, a maze "
        "of thoughts), surreal or impossible imagery, or explicitly symbolic "
        "visuals. A sentence merely being about emotions, growth, healing, or "
        "relationships in the abstract is NOT enough to qualify for C - only "
        "use C when the literal content described could not be filmed by a "
        "camera in the real world.\n\n"
        f"Sentences:\n{numbered}\n\n"
        'Respond ONLY with JSON: {"categories": ["A", "B", "C", ...]} - exact '
        "same order and count as the sentences above."
    )
    try:
        raw = call_groq(prompt)
        data = json.loads(raw)
        categories = data.get("categories", [])
        if len(categories) != len(sentences):
            print(f"[pipeline] scene category count mismatch ({len(categories)} vs "
                  f"{len(sentences)} sentences) - falling back to default routing")
            return [None] * len(sentences)
        return [c if c in ("A", "B", "C") else None for c in categories]
    except Exception as e:  # noqa: BLE001 - classifier must never abort a run
        print(f"[pipeline] scene category classification failed, falling back: {e}")
        return [None] * len(sentences)


def generate_script(topic: str, pillar: str, feedback: str = "") -> dict:
    feedback_block = ""
    if feedback:
        feedback_block = textwrap.dedent(f"""

            IMPORTANT - a previous draft on this exact topic was reviewed and
            scored too low because: "{feedback}"
            Write a genuinely different draft that specifically fixes that
            weakness, while still following every requirement below.
        """)
    tone = CONTENT_PILLARS[pillar]["tone"]
    prompt = textwrap.dedent(f"""
        You are the writer for MindByte, a YouTube channel about why humans
        think, feel and behave the way they do. The channel must feel like a
        psychology documentary crossed with a storytelling channel - NOT a
        generic facts channel, NOT a listicle, NOT a low-effort AI content
        farm.

        You are writing a 30-60 second YouTube Short. The topic is:
        "{topic}", from the "{pillar}" pillar. Tone for this pillar: {tone}.
        {feedback_block}
        STRUCTURE - tell one small story, do not dump facts:
        1. HOOK (first 1-3 seconds) - grab attention instantly.
        2. Introduce a relatable human problem or moment tied to the topic.
        3. Create a curiosity gap - make the viewer need the explanation.
        4. Explain the actual psychological reason WHY this happens.
        5. Ground it with a concrete example or situation.
        6. End on one memorable, quotable insight - not "thanks for
           watching."

        HOOK RULES - the first sentence decides whether anyone stays:
        Never start with "Welcome back", "Today we will discuss", "Did you
        know", or any greeting or announcement. Instead open with a
        surprising statement, a psychological question, an emotional
        trigger, or a curiosity gap - in your own words, not copied from
        anywhere. In style only (write your own, do not reuse these):
        "Your brain does something strange when someone ignores you...",
        "There's a psychological reason you can't stop thinking about
        someone...", "The reason confident people seem attractive isn't
        what you think...".

        Requirements:
        - ORIGINAL wording throughout - your own framing, not copied
          phrasing from any single source.
        - No clinical, diagnostic, or medical advice - general-interest
          psychology, not therapy or a diagnosis.
        - Every sentence should feel like it is talking directly to the
          viewer about THEIR own mind, not narrating a study from a
          distance.
        - Write 15-19 short, punchy sentences, each between 9 and 14 words -
          vivid and complete, not tiny fragments, and not long multi-clause
          lecture sentences either. Map the storytelling beats above across
          these sentences: roughly 1 hook sentence, 2-3 for the relatable
          problem, 2-3 building curiosity, 5-7 explaining the psychology,
          3-4 for examples, 1-2 for the closing insight.
        - HARD REQUIREMENT: total narration between 140 and 190 words.
          Count before finalizing - under 140 words is a failed response.
        - Keep the energy high throughout - rhetorical questions, quick
          reveals, or "but here's the part that changes everything" style
          pivots are welcome, but the throughline must still read as ONE
          coherent story about a psychological reason, not a list of
          disconnected facts.
        - Choose a genuinely surprising, lesser-known psychological angle -
          avoid the most obvious "psychology 101" trivia, which reads as
          generic and low-effort.
        - End with a punchy, quotable closing line the viewer would want to
          screenshot or say to a friend - not "thanks for watching" or
          "follow for more."
        - Also produce a clickable title (under 90 characters) that creates
          curiosity without being clickbait-false. Avoid formats like "5
          psychology facts" - prefer something like "Why Your Brain Makes
          You Miss Someone Who Hurt You" or "The Hidden Psychology Behind
          Human Attraction".
        - Also produce a YouTube description (2-3 sentences plus 3 relevant
          hashtags).
        - Also produce "visual_keywords": an array with EXACTLY the same
          number of entries as "sentences", in the same order - one 2-3
          word stock-video search phrase per sentence, for CINEMATIC,
          PEOPLE-FREE B-roll that fits an anime/illustrated-character
          channel: technology and futuristic machinery, robots, abstract
          particles/light, neural-network-style visuals, nature (ocean,
          forest, sky, weather), architecture and cityscapes shot WITHOUT
          pedestrians or faces in frame, close-ups of objects, clocks,
          screens, or environments. Byte (our illustrated narrator)
          appears as the "human" presence in this channel - real human
          faces/bodies in the stock footage visually clash with his
          anime style, so NEVER request queries centered on people, faces,
          hands, or human activity ("person thinking", "couple talking",
          "handshake", etc.). Prefer mood/metaphor over literal
          illustration - e.g. for a sentence about overthinking, prefer
          "tangled wires" or "spinning gears" over "person worrying".
        - Also produce "tags": an array of 10-15 SEARCH TERMS a real viewer
          would type into YouTube (NOT stock-footage descriptions) - a mix
          of broad terms ("psychology facts", "human behavior", "why
          people", "self improvement"), terms specific to "{topic}", and
          the channel name "MindByte".

        Return ONLY valid JSON with this exact shape:
        {{
        "title": "...",
        "description": "...",
        "sentences": ["...", "..."],
        "visual_keywords": ["...", "..."],
        "tags": ["...", "..."]
        }}
    """).strip()
    raw = call_groq(prompt)
    data = json.loads(raw)
    # Defensive: guarantee 1:1 sentence/keyword pairing even if the model
    # drifts from the requested shape, since assembly depends on it.
    sentences = data.get("sentences", [])
    keywords = data.get("visual_keywords", [])
    if len(keywords) < len(sentences):
        keywords = keywords + [data.get("title", "")] * (len(sentences) - len(keywords))
    elif len(keywords) > len(sentences):
        keywords = keywords[: len(sentences)]
    data["visual_keywords"] = keywords
    # Defensive: fall back to the visual keywords (still better than nothing)
    # if the model omits "tags" entirely, so upload never crashes on a
    # missing field.
    if not data.get("tags"):
        data["tags"] = keywords
    return data

def score_quality(topic: str, pillar: str, script: dict) -> dict:
    prompt = textwrap.dedent(f"""
        Rate this YouTube Shorts script for MindByte, a psychology /
        human-behavior channel positioned as a documentary/storytelling
        channel - NOT a generic facts channel, listicle, or low-effort AI
        content farm.

        This is a fast, punchy 30-60 second VERTICAL SHORT told as ONE
        small story with short, energetic sentences BY DESIGN - do not
        penalize short sentences or a fast pace, that is the correct
        format for this channel. DO penalize it if it reads as a
        disconnected list of facts rather than one coherent story
        explaining WHY the behavior happens.

        Score primarily on:
        - Hook strength: does the first sentence grab attention
          immediately, without a generic opener?
        - Storytelling: does it read as one coherent narrative (relatable
          problem -> curiosity -> psychological explanation -> example ->
          insight), not a fact dump?
        - Emotional pull: would a viewer think "that explains me" or "I
          never realized this"?
        - Originality: no generic filler like "did you know" or "stay
          tuned to find out", and the psychological angle is not the most
          obvious 101-level trivia.
        - Shareability: is the closing line memorable or quotable enough
          that someone would send this to a friend?

        Only score below 6 if the script is genuinely boring, generic, or
        reads like a facts-channel list rather than a story.

        Topic: {topic} (pillar: {pillar})
        Title: {script['title']}
        Script: {" ".join(script['sentences'])}

        Return ONLY valid JSON: {{"score": <integer 1-10>, "notes": "<one
        sentence justification>"}}
    """).strip()
    raw = call_groq(prompt)
    return json.loads(raw)

MIN_SCRIPT_WORDS = 130  # keeps narration filling the 45-55s target instead of drifting to ~30s


def generate_and_score_script(topic: str, pillar: str, max_attempts: int = MAX_SCRIPT_ATTEMPTS) -> tuple:
    """Generate + score a script, retrying with feedback if it falls short
    of the quality bar OR is too short to fill the target duration, so a
    single pipeline run gets multiple shots at clearing both bars instead
    of failing outright (or silently landing short) on one weak first draft.

    Videos were consistently landing at 30-35s despite the prompt asking
    for 45-55s (~130-160 words) - the quality score alone doesn't catch
    this, since a short script can still score well on hook/pacing. This
    adds an explicit word-count floor to the retry decision, on top of the
    existing quality check, so a script that's high-scoring but too short
    gets sent back for another attempt with specific feedback instead of
    being accepted as-is.

    Returns the (script, quality) pair that best satisfies both bars, or
    the best-scoring one seen if no attempt clears both within the budget.
    """
    best_script, best_quality, best_meets_bar, best_word_count = (
        None,
        {"score": -1, "notes": ""},
        False,
        -1,
    )
    feedback = ""
    for attempt in range(1, max_attempts + 1):
        script = generate_script(topic, pillar, feedback=feedback)
        quality = score_quality(topic, pillar, script)
        word_count = sum(len(s.split()) for s in script["sentences"])
        meets_bar = quality["score"] >= QUALITY_THRESHOLD and word_count >= MIN_SCRIPT_WORDS
        print(
            f"[pipeline] attempt {attempt}/{max_attempts}: "
            f"quality score {quality['score']} - {quality['notes']} "
            f"(word count: {word_count})"
        )
        is_better = (
            (meets_bar and not best_meets_bar)
            or (meets_bar == best_meets_bar and quality["score"] > best_quality["score"])
            or (
                meets_bar == best_meets_bar
                and quality["score"] == best_quality["score"]
                and word_count > best_word_count
            )
        )
        if best_script is None or is_better:
            best_script, best_quality, best_meets_bar, best_word_count = (
                script,
                quality,
                meets_bar,
                word_count,
            )
        if meets_bar:
            break
        if quality["score"] >= QUALITY_THRESHOLD:
            feedback = (
                f"the script scored well but was only {word_count} words - too "
                f"short to fill 45-55 seconds. Write at least {MIN_SCRIPT_WORDS} "
                f"words this time by adding 2-3 more surprising beats, while "
                f"keeping the same punchy short-sentence style."
            )
        else:
            feedback = quality.get("notes", "")
    return best_script, best_quality

def compliance_check(script: dict) -> dict:
    """Lightweight originality/licensing check.

    We only ever use Pexels-licensed footage and machine-generated
    narration for this channel, so the licensing side is compliant by
    construction. This check focuses on the one thing that can still go
    wrong: templated, near-duplicate scripts. There is no public API for
    genuine pre-upload Content-ID-style scanning - this is NOT a guarantee
    against copyright claims, just a basic originality/policy sanity check.
    """
    generic_phrases = [
        "did you know that", "in this video we will", "welcome back to my channel",
        "smash that like button", "don't forget to subscribe",
        "today we will discuss", "in today's video", "stay tuned to find out",
    ]
    lowered = " ".join(script["sentences"]).lower()
    flags = [p for p in generic_phrases if p in lowered]
    passed = len(flags) == 0
    notes = "OK" if passed else f"Generic phrasing detected: {', '.join(flags)}"
    return {"passed": passed, "notes": notes}


# ---------------------------------------------------------------------------
# Pexels
# ---------------------------------------------------------------------------

def search_pexels_clip(query: str, used_ids: set) -> dict | None:
    resp = SESSION.get(
        "https://api.pexels.com/videos/search",
        headers={"Authorization": PEXELS_API_KEY},
        params={"query": query, "orientation": "portrait", "per_page": 15},
        timeout=30,
    )
    if resp.status_code != 200:
        return None
    for video in resp.json().get("videos", []):
        if video["id"] in used_ids:
            continue
        # Prefer an HD portrait file.
        files = sorted(
            video["video_files"],
            key=lambda f: (f.get("width") or 0) * (f.get("height") or 0),
            reverse=True,
        )
        for f in files:
            if f.get("width") and f.get("height") and f["height"] >= f["width"]:
                used_ids.add(video["id"])
                return {"id": video["id"], "url": f["link"]}
        # Fall back to the largest available file if no portrait file exists.
        if files:
            used_ids.add(video["id"])
            return {"id": video["id"], "url": files[0]["link"]}
    return None


def download_file(url: str, dest_path: str) -> None:
    resp = SESSION.get(url, stream=True, timeout=120)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1 << 20):
            f.write(chunk)


FALLBACK_QUERIES = [
    # People-free, cinematic B-roll only (2026-07-23 direction: the channel's
    # only "human" presence should be Byte's illustrated character - real
    # people in stock clips visually clash with his anime style). Kept to
    # nature/tech/abstract/architecture themes, deliberately excluding any
    # query that tends to surface pedestrians, hands, or faces.
    "nature", "abstract background", "city timelapse (no people)",
    "clouds timelapse", "ocean waves", "forest aerial", "starry sky",
    "futuristic technology", "robot machinery", "neural network abstract",
    "circuit board macro", "particles light abstract", "gears machinery",
    "empty architecture", "rain window", "clock close up",
]


# --- Character asset system (new) ---
# Illustrated character/environment clips are rendered in gather_clips(),
# BEFORE the real per-sentence voiceover duration is known (that comes from
# generate_voiceover_segments(), which runs later in main()). assemble_video()
# later trims every clip down to its real segment duration with `-t`, which
# can only shorten a source clip, never lengthen one. So these illustrated
# clips must be rendered at least as long as the longest realistic spoken
# sentence could run, or a longer-than-expected sentence would trim past the
# end of a too-short source clip and the video would visibly end/freeze
# before the audio does. 15s comfortably covers any single TTS sentence this
# pipeline generates.
CHARACTER_CLIP_SAFE_DURATION = 15.0


def _character_image_to_clip(image_path: str, dest_path: str, duration: float = 3.0) -> bool:
    """Convert a still character illustration into a short silent mp4 clip
    so it can slot into clip_paths exactly like a downloaded Pexels clip
    (assemble_video() trims every clip_paths[i] to segment_durations[i] via
    plain -i/-t, which needs a looped image source, not a single frame).
    Returns True on success, False on any ffmpeg failure (caller should
    treat that the same as "no asset found" and fall back to Pexels)."""
    try:
        frames = max(1, int(duration * 30))
        # Slow, subtle Ken Burns zoom (1.0 -> ~1.08x over the clip) so a
        # held illustration reads as a deliberate cinematic shot rather
        # than a static slideshow image - directly addresses the standing
        # "must not feel like a slideshow" requirement for illustrated
        # character beats, same as real B-roll clips already have motion.
        # Source character art comes in mixed aspect ratios (portrait
        # reference shots, landscape scene shots), so scale-to-cover +
        # center-crop to the target 1080x1920 frame before zoompan, rather
        # than a plain scale that can leave one dimension too small for
        # zoompan/crop to work with.
        zoompan = (
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,"
            f"zoompan=z='min(zoom+0.0015,1.08)':d={frames}:s=1080x1920:fps=30"
        )
        base_path = dest_path + ".base.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-i", image_path,
             "-c:v", "libx264", "-preset", "medium", "-crf", "18",
             "-pix_fmt", "yuv420p", "-vf", zoompan, base_path],
            check=True, capture_output=True,
        )
        # Give Byte's own shots the same subtle living/flicker motion as the
        # illustrated Environment backgrounds (lighter opacity here since a
        # character close-up shouldn't get as much atmosphere grain as a wide
        # establishing shot) - whichever asset type a beat lands on, the
        # on-screen result should read as similarly alive, not just the
        # backgrounds. Falls back to the plain Ken Burns clip if the shared
        # overlay asset isn't present or ffmpeg fails for any reason.
        if apply_atmosphere_overlay(base_path, dest_path, duration, opacity=0.18):
            os.remove(base_path)
        else:
            os.replace(base_path, dest_path)
        return True
    except (subprocess.CalledProcessError, OSError):
        return False
# --- end character asset system (new) ---


def gather_clips(keywords: list, workdir: str, sentences: list = None, scene_categories: list = None) -> tuple:
    """Download exactly one clip per keyword, in order.

    `scene_categories`, if provided (same length/order as `keywords`), is the
    per-beat A/B/C classification from classify_scene_categories(). Category
    "A" (pure everyday stock footage) skips the illustrated-character-asset
    attempt entirely, so a beat like "walking down a city street" doesn't
    accidentally get Byte pasted into a generic B-roll shot that was never
    meant to feature him. Categories "B"/"C" and None (classifier
    unavailable/skipped this slot) all go through the existing character-
    asset-first logic unchanged - see the module-level A/B/C comment block
    above classify_scene_categories() for why B and C share handling for now.

    A strict 1:1, order-preserving mapping between sentences and clips is
    required so assemble_video can cut to a new clip exactly when each
    sentence is spoken, instead of cutting on an unrelated fixed timer.
    If a specific keyword yields nothing on any stock source, fall back
    through a rotation of generic queries for that slot rather than
    skipping it, so the clip count never drifts out of sync with the
    sentence count.

    `sentences`, if provided, is the full spoken-line text for each slot
    (same length/order as `keywords`). It's used ONLY for the character-
    asset match below, since a 2-3 word stock-footage search phrase
    ("person thinking bedroom") almost never contains a character's
    narrator-style personality_keywords ("here's why", "explain") the way
    the actual spoken sentence does - matching against keyword text alone
    made this feature effectively dead code. Stock search itself still
    uses only `keyword`, unchanged.

    Returns (clip_paths, stock_attributions) - the latter is a list of
    credit-line strings for any non-CC0 stock source used (currently just
    Coverr's free-tier attribution requirement), for main() to fold into
    the video description the same way non-CC0 background-music credits
    already are.
    """
    used_ids: set = set()
    used_character_files: set = set()
    clip_paths = []
    stock_attributions: list = []
    for i, keyword in enumerate(keywords):
        category = scene_categories[i] if scene_categories and i < len(scene_categories) else None
        # --- Character asset system (new) ---
        # Try an illustrated-character asset for this slot before touching
        # stock at all - UNLESS this beat was classified as Category A (pure
        # everyday stock footage), in which case Byte should never appear
        # here at all. select_character_asset() returns None whenever no
        # character/asset matches (or assets aren't present on disk yet),
        # in which case we fall through to the stock logic below unchanged.
        sentence_text = sentences[i] if sentences and i < len(sentences) else ""
        match_text = f"{keyword} {sentence_text}".strip()
        char_asset = None if category == "A" else select_character_asset(
            match_text, CHARACTERS_MANIFEST, exclude_files=used_character_files
        )
        if char_asset:
            dest = os.path.join(workdir, f"clip_{i}.mp4")
            if char_asset["asset_type"] == "Environments":
                # Illustrated background plate (dark bedroom, rain-lit street,
                # etc.) - render with the camera-push + atmosphere-overlay
                # motion treatment instead of the flat character-cutout Ken
                # Burns, since these are meant to read as cinematic "Mind
                # Layer" scenes, not talking-head cutaways.
                #
                # IMPORTANT: gather_clips() runs BEFORE generate_voiceover_segments(),
                # so the real per-sentence audio duration for this slot isn't
                # known yet - assemble_video() trims this clip down to that
                # real duration later with `-t`, which can only shorten a
                # clip, never extend one. Rendering at a short 3-4s default
                # (the old behavior) meant any sentence whose real spoken
                # duration ran longer left this segment ending early while
                # the audio kept playing - exactly the "video doesn't end
                # correctly" symptom reported after the first test video.
                # Rendering generously long (CHARACTER_CLIP_SAFE_DURATION)
                # guarantees there's always enough source to trim down from.
                try:
                    render_environment_motion_clip(char_asset["path"], dest, duration=CHARACTER_CLIP_SAFE_DURATION)
                    clip_paths.append(dest)
                    used_character_files.add(char_asset["filename"])
                    continue
                except Exception:
                    pass  # fall through to Pexels below on any render failure
            elif _character_image_to_clip(char_asset["path"], dest, duration=CHARACTER_CLIP_SAFE_DURATION):
                clip_paths.append(dest)
                used_character_files.add(char_asset["filename"])
                continue
        # --- end character asset system (new) ---
        # Category A (everyday stock environments): try Pexels, then Pixabay,
        # then Coverr, in order, per the "don't depend on a single source"
        # direction - Mixkit and Videvo are deliberately excluded, see
        # stock_sources.py's module docstring for why (ToS/licensing).
        clip = search_multi_source_clip(keyword, used_ids, search_pexels_clip)
        if not clip:
            for fb in FALLBACK_QUERIES:
                clip = search_multi_source_clip(fb, used_ids, search_pexels_clip)
                if clip:
                    break
        if not clip:
            continue
        dest = os.path.join(workdir, f"clip_{i}.mp4")
        download_file(clip["url"], dest)
        clip_paths.append(dest)
        if clip.get("attribution"):
            stock_attributions.append(clip["attribution"])
    return clip_paths, stock_attributions


# ---------------------------------------------------------------------------
# Background music (Openverse - free, no API key, commercially-licensed)
# ---------------------------------------------------------------------------

OPENVERSE_AUDIO_URL = "https://api.openverse.org/v1/audio/"
MUSIC_QUERIES = [
    # Generic fallback used only if a pillar has no music_queries or all of
    # its queries come up empty on Openverse (content-strategy phase 2,
    # 2026-07-19) - normal picks now come from CONTENT_PILLARS[pillar]["music_queries"].
    "mysterious ambient", "cinematic tension calm", "curious ambient piano",
    "ambient technology", "dark ambient minimal", "inspiring ambient",
]
MIN_MUSIC_DURATION_MS = 30000  # skip very short stingers that can't cover a full clip
MUSIC_VOLUME = 0.14  # lowered from 0.22 (2026-07-23) - user found the louder bed
# distracting under narration; 0.14 keeps a felt-but-not-noticed ambient bed.
# (History: was 0.15 originally, bumped to 0.22 on 2026-07-19 because music was
# too quiet, now brought back down because loud enough to distract - the goal
# is present-but-unobtrusive, not loud.)

# ---------------------------------------------------------------------------
# Visual branding (content-strategy branding pass, 2026-07-19) - a
# consistent MindByte identity burned into every video: a branded title
# card covering the first TITLE_CARD_SECONDS (replacing a cold open
# straight onto generic stock footage), a small persistent corner
# watermark for the whole video, and a subtle documentary-style color
# grade applied to every clip. The original uploaded channel-art PNGs
# (profile picture/banner) aren't files in this environment, so the logo
# mark is recreated procedurally here from the same "dots and lines"
# neuron-network motif, matching the deep indigo-to-violet brand
# gradient already live on the channel.
# ---------------------------------------------------------------------------
BRAND_BG_TOP = (26, 18, 66)       # deep indigo
BRAND_BG_BOTTOM = (91, 46, 143)   # violet
BRAND_TEXT_COLOR = (255, 255, 255)
BRAND_NAME = "MindByte"
TITLE_CARD_SECONDS = 1.1
WATERMARK_SIZE = 90

# Per-pillar accent color for the title card's divider bar - kept as a
# separate dict (rather than added into CONTENT_PILLARS) so the
# existing, already-validated pillar structure doesn't need to change.
PILLAR_ACCENT_COLORS = {
    "Relationship Psychology": (224, 122, 149),
    "Human Behavior Psychology": (122, 178, 224),
    "Social Psychology": (240, 180, 90),
    "Brain & Neuroscience": (110, 200, 200),
    "Emotional Intelligence": (150, 200, 140),
    "Psychology Experiments & Stories": (170, 130, 220),
}

def _brand_font(size: int):
    """Loads a bold sans font for branding text, trying a couple of
    common paths (both standard on GitHub Actions' ubuntu-latest
    runners) before falling back to Pillow's basic default font so a
    missing font file can never crash a run."""
    for path in (
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()

def _draw_logo_mark(draw, cx: float, cy: float, radius: float,
                     color=(255, 255, 255)) -> None:
    """Draws MindByte's small neuron-network mark - a center dot with
    five connected satellite dots - procedurally, since the actual
    uploaded logo PNG isn't a file in this environment. Reused for both
    the title card and the small corner watermark."""
    draw.ellipse(
        [cx - radius * 0.28, cy - radius * 0.28, cx + radius * 0.28, cy + radius * 0.28],
        fill=color,
    )
    for i in range(5):
        angle = math.pi * 2 * i / 5 - math.pi / 2
        sx, sy = cx + math.cos(angle) * radius, cy + math.sin(angle) * radius
        draw.line([cx, cy, sx, sy], fill=color, width=max(2, int(radius * 0.05)))
        dot_r = radius * 0.12
        draw.ellipse([sx - dot_r, sy - dot_r, sx + dot_r, sy + dot_r], fill=color)

def build_watermark_png(dest_path: str) -> None:
    """Small persistent corner brand mark, generated once per run and
    burned into every video for its full duration via an ffmpeg overlay
    in assemble_video(). Kept small and placed top-left, clear of both
    the caption zone (MarginV=420 from the bottom) and the Shorts UI's
    own bottom title block + right-edge icon column."""
    img = Image.new("RGBA", (WATERMARK_SIZE, WATERMARK_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse(
        [2, 2, WATERMARK_SIZE - 2, WATERMARK_SIZE - 2],
        fill=(*BRAND_BG_TOP, 140),
    )
    _draw_logo_mark(draw, WATERMARK_SIZE / 2, WATERMARK_SIZE / 2, WATERMARK_SIZE * 0.32,
                     color=(255, 255, 255, 230))
    img.save(dest_path)

def build_title_card(dest_path: str, title: str, pillar: str) -> None:
    """Generates the branded intro frame shown for TITLE_CARD_SECONDS at
    the start of every video (content-strategy branding pass,
    2026-07-19), replacing a cold open straight onto generic stock
    B-roll with a consistent, premium documentary-style title card: a
    vertical brand-color gradient, the MindByte mark + wordmark, a thin
    pillar-accent divider, and the episode's own title. Verified locally
    with a synthetic render before this went live - see the branding
    pass notes in the project doc."""
    width, height = VIDEO_WIDTH, VIDEO_HEIGHT
    img = Image.new("RGB", (width, height), BRAND_BG_TOP)
    draw = ImageDraw.Draw(img)
    for y in range(height):
        t = y / height
        r = int(BRAND_BG_TOP[0] + (BRAND_BG_BOTTOM[0] - BRAND_BG_TOP[0]) * t)
        g = int(BRAND_BG_TOP[1] + (BRAND_BG_BOTTOM[1] - BRAND_BG_TOP[1]) * t)
        b = int(BRAND_BG_TOP[2] + (BRAND_BG_BOTTOM[2] - BRAND_BG_TOP[2]) * t)
        draw.line([(0, y), (width, y)], fill=(r, g, b))

    accent = PILLAR_ACCENT_COLORS.get(pillar, (255, 255, 255))
    logo_cy = int(height * 0.30)
    _draw_logo_mark(draw, width / 2, logo_cy, width * 0.07, color=(255, 255, 255))

    wordmark_font = _brand_font(int(width * 0.075))
    wm_bbox = draw.textbbox((0, 0), BRAND_NAME, font=wordmark_font)
    wm_w = wm_bbox[2] - wm_bbox[0]
    draw.text((width / 2 - wm_w / 2, logo_cy + width * 0.11), BRAND_NAME,
              font=wordmark_font, fill=BRAND_TEXT_COLOR)

    bar_y = int(height * 0.52)
    bar_w = int(width * 0.22)
    draw.rectangle([width / 2 - bar_w / 2, bar_y, width / 2 + bar_w / 2, bar_y + 6], fill=accent)

    title_font = _brand_font(int(width * 0.062))
    words = title.split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textbbox((0, 0), trial, font=title_font)[2] > width * 0.82 and cur:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    if cur:
        lines.append(cur)
    lines = lines[:3]
    line_h = int(width * 0.085)
    ty = bar_y + 40
    for line in lines:
        lb = draw.textbbox((0, 0), line, font=title_font)
        lw = lb[2] - lb[0]
        draw.text((width / 2 - lw / 2, ty), line, font=title_font, fill=(255, 255, 255))
        ty += line_h

    img.save(dest_path)


def build_thumbnail(dest_path: str, title: str, pillar: str) -> None:
    """Generates a custom branded thumbnail/cover frame for the Short,
    uploaded after the video goes live via set_youtube_thumbnail().
    Distinct from build_title_card() (a brief in-video intro) - weighted
    toward a large, bold curiosity-driving title plus a simple pointing
    accent device. Borrowed as a PRINCIPLE only (never a copy) from the
    competitor research: TED-Ed's consistent color-block thumbnail
    template and Kurzgesagt's pointing-arrow device. A missing custom
    thumbnail/cover treatment was flagged as the single biggest, most
    obvious first-frame gap versus the researched competitor channels -
    this is the phase-3 follow-up fix for that gap."""
    width, height = VIDEO_WIDTH, VIDEO_HEIGHT
    img = Image.new("RGB", (width, height), BRAND_BG_TOP)
    draw = ImageDraw.Draw(img)
    for y in range(height):
        f = y / height
        r = int(BRAND_BG_TOP[0] + (BRAND_BG_BOTTOM[0] - BRAND_BG_TOP[0]) * f)
        g = int(BRAND_BG_TOP[1] + (BRAND_BG_BOTTOM[1] - BRAND_BG_TOP[1]) * f)
        b = int(BRAND_BG_TOP[2] + (BRAND_BG_BOTTOM[2] - BRAND_BG_TOP[2]) * f)
        draw.line([(0, y), (width, y)], fill=(r, g, b))

    accent = PILLAR_ACCENT_COLORS.get(pillar, (255, 255, 255))

    title_font = _brand_font(int(width * 0.11))
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

    line_h = int(width * 0.135)
    block_h = line_h * len(lines)
    ty = int(height * 0.40) - block_h // 2
    for line in lines:
        lb = draw.textbbox((0, 0), line, font=title_font)
        lw = lb[2] - lb[0]
        draw.text((width / 2 - lw / 2, ty), line, font=title_font, fill=(255, 255, 255))
        ty += line_h

    bar_y = ty + 10
    bar_w = int(width * 0.30)
    draw.rectangle([width / 2 - bar_w / 2, bar_y, width / 2 + bar_w / 2, bar_y + 8], fill=accent)

    ax = int(width * 0.5)
    ay = bar_y + int(height * 0.05)
    arrow_size = int(width * 0.05)
    draw.polygon(
        [
            (ax - arrow_size, ay),
            (ax + arrow_size, ay),
            (ax, ay + int(arrow_size * 1.3)),
        ],
        fill=accent,
    )

    logo_cy = int(height * 0.88)
    logo_cx = int(width * 0.40)
    logo_r = width * 0.035
    _draw_logo_mark(draw, logo_cx, logo_cy, logo_r, color=(255, 255, 255))
    small_font = _brand_font(int(width * 0.045))
    draw.text((logo_cx + logo_r + 14, logo_cy - int(width * 0.028)), BRAND_NAME,
              font=small_font, fill=BRAND_TEXT_COLOR)

    img.save(dest_path)


def fetch_background_music(dest_path: str, pillar: str) -> dict | None:
    """Best-effort fetch of a free, commercially-licensed instrumental track
    from Openverse (aggregates Jamendo, Free Music Archive, etc. - no API
    key needed). This is a nice-to-have: any failure (network, no results,
    license mismatch) is swallowed and logged so a music-fetch problem never
    aborts video production - the video is still fine without a music bed.

    Query is now picked from CONTENT_PILLARS[pillar]["music_queries"]
    (content-strategy phase 2, 2026-07-19) so the mood matches the
    content - emotional piano for Relationship Psychology, futuristic
    ambient for Brain & Neuroscience, subtle tension for Psychology
    Experiments & Stories, etc. - falling back to the generic
    MUSIC_QUERIES list if the pillar has none or nothing is found.

    IMPORTANT (fixed 2026-07-23): previously this tried exactly ONE
    randomly-chosen query and gave up entirely - returning None - the
    moment that single query had no results, which is why run #37
    published with no music at all even though Openverse almost
    certainly had SOMETHING usable under a different query. Now it
    tries every pillar query, then every generic fallback query (in a
    shuffled, deduplicated order), and only gives up after all of them
    come up empty - music should be the exception-not-the-rule case,
    not "first query fails -> silent video."
    """
    pillar_queries = CONTENT_PILLARS.get(pillar, {}).get("music_queries") or []
    # Try the pillar's own mood-matched queries first, then fall back to the
    # generic list - dedup while preserving order, then shuffle each group
    # independently so repeated runs don't always hammer the same query first.
    seen = set()
    ordered_queries = []
    for group in (pillar_queries, MUSIC_QUERIES):
        group = list(group)
        random.shuffle(group)
        for q in group:
            if q not in seen:
                seen.add(q)
                ordered_queries.append(q)

    for query in ordered_queries:
        try:
            resp = SESSION.get(
                OPENVERSE_AUDIO_URL,
                params={"q": query, "license_type": "commercial", "page_size": 10},
                timeout=20,
            )
            if resp.status_code != 200:
                print(f"[pipeline] music search failed for '{query}': {resp.status_code} {resp.text[:200]}")
                continue
            results = resp.json().get("results", [])
            candidates = [
                r for r in results
                if r.get("url") and (r.get("duration") or 0) >= MIN_MUSIC_DURATION_MS
            ]
            if not candidates:
                print(f"[pipeline] no suitable music candidates for query '{query}', trying next query")
                continue
            track = random.choice(candidates)
            download_file(track["url"], dest_path)
            return {
                "title": track.get("title") or "Untitled",
                "creator": track.get("creator") or "Unknown artist",
                "license": (track.get("license") or "unknown").lower(),
            }
        except Exception as e:  # noqa: BLE001 - deliberately broad, see docstring
            print(f"[pipeline] music fetch failed for '{query}', trying next query: {e}")
            continue

    print(f"[pipeline] no suitable music found across {len(ordered_queries)} queries - continuing without music")
    return None

def mix_background_music(voice_path: str, music_path: str, duration: float,
                          dest_path: str) -> None:
    """Loop the music bed to cover the narration, duck its volume well
    under the voice, and mix the two into a single audio track.

    On top of the low MUSIC_VOLUME, the music bed is gently EQ'd (highpass
    to drop rumble, lowpass to tame bright/percussive transients that
    otherwise poke through under the voice) and lightly compressed so it
    reads as a soft ambient texture rather than a competing second layer -
    "present but unobtrusive" per user feedback that the previous mix was
    distracting under narration.
    """
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-stream_loop", "-1", "-i", music_path,
            "-i", voice_path,
            "-filter_complex",
            f"[0:a]atrim=0:{duration:.3f},"
            "highpass=f=150,lowpass=f=6000,"
            f"volume={MUSIC_VOLUME},"
            "acompressor=threshold=0.1:ratio=4:attack=20:release=250[music];"
            f"[1:a]volume=1.0[voice];"
            f"[music][voice]amix=inputs=2:duration=longest:dropout_transition=2[aout]",
            "-map", "[aout]", "-t", f"{duration:.3f}",
            "-c:a", "libmp3lame", "-q:a", "4",
            dest_path,
        ],
        check=True, capture_output=True,
    )

def master_audio(src_path: str, dest_path: str, duration: float) -> None:
    """Light mastering pass applied to the FINAL narration track (with
    or without background music already mixed in) right before it's
    muxed into the video - added 2026-07-19 after the user noticed
    MindByte's audio sounded noticeably rougher than other channels'. A
    highpass removes low-end rumble edge-tts sometimes leaves in, a
    gentle compressor evens out loudness swings between louder/quieter
    sentences, and a loudnorm pass brings the whole track to a
    consistent, broadcast-standard loudness (YouTube's own -14 LUFS
    target) instead of whatever level edge-tts/the mix happened to
    produce - for a free-TTS pipeline, this one step tends to be the
    single biggest lever toward sounding "produced" rather than raw.
    Wrapped in try/except by the caller so a mastering failure can never
    block a run - the unmastered track is still perfectly usable.
    """
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", src_path, "-t", f"{duration:.3f}",
            "-af",
            "highpass=f=80,"
            "acompressor=threshold=-18dB:ratio=2:attack=5:release=50,"
            "loudnorm=I=-14:TP=-1.5:LRA=11",
            "-c:a", "libmp3lame", "-q:a", "2",
            dest_path,
        ],
        check=True, capture_output=True,
    )


# ---------------------------------------------------------------------------
# Voiceover (edge-tts) + captions
# ---------------------------------------------------------------------------

async def _synthesize(text: str, dest_path: str, voice: str = "en-US-AriaNeural",
                       rate: str = "+10%", pitch: str = "+3Hz") -> None:
    import edge_tts
    # A faster rate and a slightly raised pitch push the narration away
    # from a flat, lecture-like delivery toward the punchier, higher-energy
    # pace typical of Shorts (user feedback: first cut "sounded like a
    # lecture"). Paired with the punchier/shorter script prompt above.
    communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
    await communicate.save(dest_path)


def _sentence_prosody(sentence: str, index: int, total: int,
                       base_rate: int = 10, base_pitch: int = 3) -> tuple:
    """Pick a rate/pitch for THIS sentence instead of reusing the same
    fixed rate/pitch for every line.

    Even after splitting narration into per-sentence clips (which fixed
    the choppy pacing), every clip still used the exact same rate and
    pitch - so the voice itself still sounded like one flat, robotic
    read-through (user feedback: "audio still not right"). Real speech
    changes pace and pitch depending on what's being said: a hook opens
    slower and lower to pull you in, questions lift up, and a punchline
    or final line lands faster and higher for energy. This function
    fakes that by picking a different rate/pitch per sentence based on
    its role (first line, question, last line, middle line) plus a
    small random jitter so back-to-back "normal" sentences don't sound
    identical either.

    base_rate/base_pitch (2026-07-19, content-strategy phase 2) let the
    caller shift the WHOLE sentence's baseline per pillar - e.g. a
    Relationship Psychology script reads a little warmer/slower, a
    Psychology Experiments & Stories script reads a little deeper and
    more deliberate - while this function's existing per-sentence-role
    logic still layers hook/question/closing-line variation on top.
    """
    if index == 0:
        # The hook: pull back slightly, land it more deliberately.
        rate, pitch = base_rate - 5, base_pitch - 2
    elif index == total - 1:
        # The payoff/last line: push forward with more energy.
        rate, pitch = base_rate + 7, base_pitch + 5
    elif sentence.strip().endswith("?"):
        # Questions lift in pitch and ease off pace slightly.
        rate, pitch = base_rate - 2, base_pitch + 4
    else:
        rate, pitch = base_rate, base_pitch

    rate += random.randint(-3, 3)
    pitch += random.randint(-2, 2)
    return f"{rate:+d}%", f"{pitch:+d}Hz"

SENTENCE_GAP_MS = 220  # brief pause between beats


def ffprobe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def generate_voiceover_segments(sentences: list, workdir: str, pillar: str) -> tuple:
    """Synthesize each sentence as its OWN edge-tts clip and splice them
    together with a short silence gap, instead of one Communicate() call
    over the whole script joined into a paragraph.

    A single combined call produces one continuous, flat prosody contour
    across the entire script - it reads as someone reading straight
    through a paragraph rather than distinct punchy beats (user feedback:
    "audio flow not matching story like its like someone constantly
    reading"). Synthesizing per sentence resets intonation at each
    boundary, and the inserted gap gives the ear a natural beat break.

    This also means caption/cut timing can use the REAL measured duration
    of each sentence's audio instead of a word-count estimate, which is
    more accurate than the previous compute_segment_durations() approach.

    The voice itself, and the baseline rate/pitch _sentence_prosody()
    varies around, both now come from CONTENT_PILLARS[pillar]
    (content-strategy phase 2, 2026-07-19) so a Relationship Psychology
    script sounds warmer, a Psychology Experiments & Stories script
    sounds deeper/more documentary, etc., instead of every topic using
    the same fixed voice.

    Returns (combined_audio_path, segment_durations) where
    segment_durations[i] is the real duration (seconds, including the
    trailing gap) of sentences[i]'s audio.
    """
    import asyncio

    preset = CONTENT_PILLARS.get(pillar, {})
    voice = preset.get("voice", "en-US-AriaNeural")
    base_rate = preset.get("base_rate", 10)
    base_pitch = preset.get("base_pitch", 3)

    clip_paths = []
    for i, sentence in enumerate(sentences):
        clip_path = os.path.join(workdir, f"voice_{i}.mp3")
        rate, pitch = _sentence_prosody(sentence, i, len(sentences), base_rate, base_pitch)
        asyncio.run(_synthesize(sentence, clip_path, voice=voice, rate=rate, pitch=pitch))
        clip_paths.append(clip_path)

    gap_seconds = SENTENCE_GAP_MS / 1000
    segment_durations = [ffprobe_duration(p) + gap_seconds for p in clip_paths]

    silence_path = os.path.join(workdir, "silence.mp3")
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
            "-t", f"{gap_seconds:.3f}", "-c:a", "libmp3lame", "-q:a", "4",
            silence_path,
        ],
        check=True, capture_output=True,
    )

    concat_list = os.path.join(workdir, "voice_concat.txt")
    with open(concat_list, "w") as f:
        for i, p in enumerate(clip_paths):
            f.write(f"file '{os.path.abspath(p)}'\n")
            if i < len(clip_paths) - 1:
                f.write(f"file '{os.path.abspath(silence_path)}'\n")

    combined_path = os.path.join(workdir, "voiceover.mp3")
    # Re-encode (not stream copy) since edge-tts output and the
    # ffmpeg-generated silence clip may not share identical encoding
    # parameters, which can glitch a stream-copy concat.
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list,
            "-c:a", "libmp3lame", "-q:a", "4", combined_path,
        ],
        check=True, capture_output=True,
    )
    return combined_path, segment_durations

# Emotionally-loaded words to visually highlight in burned-in captions
# (content-strategy phase 2, 2026-07-19: "Highlight important emotional
# words" - e.g. LOVE, FEAR, REJECTION, BRAIN, CONTROL, ATTRACTION - the
# strategy's example list plus a broader set covering all 6 pillars, not
# just relationship topics). Matching is case-insensitive against the
# word with punctuation stripped.
HIGHLIGHT_KEYWORDS = {
    "love", "fear", "rejection", "brain", "control", "attraction",
    "anxiety", "dopamine", "jealousy", "trust", "betrayal", "validation",
    "power", "obsession", "trauma", "insecurity", "connection", "lonely",
    "loneliness", "confidence", "manipulation", "attachment", "heartbreak",
    "desire", "shame", "guilt", "anger", "empathy", "memory", "habit",
    "addiction", "instinct", "subconscious", "willpower", "courage",
    "vulnerable", "vulnerability", "intimacy", "distrust", "denial",
    "overthinking", "procrastinate", "procrastination", "influence",
    "persuasion", "charisma", "status", "bias", "irrational", "avoid",
    "ignore", "trigger", "crave", "obsess", "manipulate", "reject",
    "chase", "hurt", "betray", "overwhelm", "distract", "seduce",
    "judge", "conform", "dominance", "insecure", "belonging",
}

# ASS override-tag colors for the highlight (BGR hex, no alpha) - a warm
# amber/gold against the base white caption color, so highlighted words
# visually pop without changing the overall caption style.
HIGHLIGHT_ASS_COLOR = "07C1FF"  # amber/gold
BASE_ASS_COLOR = "FFFFFF"  # matches the Style line's PrimaryColour (white)

# Words to never pick for the highlight-fallback below (function words carry
# no visual "punch" even when they happen to be the longest word in a short
# caption chunk).
_HIGHLIGHT_FALLBACK_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "this",
    "that", "these", "those", "your", "you", "our", "we", "it", "its", "of",
    "to", "in", "on", "for", "with", "as", "at", "by", "from", "be", "been",
    "being", "has", "have", "had", "do", "does", "did", "will", "would",
    "can", "could", "should", "not", "so", "if", "than", "then", "there",
    "here", "just", "really", "very", "quite", "almost", "into", "out",
    "about", "over", "under", "when", "while", "what", "why", "how",
}


def _highlight_ass_text(sentence: str, force_highlight: bool = True) -> str:
    """Uppercase and color a word within a caption chunk using inline ASS
    override tags, e.g. "people often avoid difficult conversations" becomes
    "people often {\\c&H07C1FF&}AVOID{\\c&HFFFFFF&} difficult conversations"
    if "avoid" is on the HIGHLIGHT_KEYWORDS list. A plain SRT file can't do
    this (force_style only applies one uniform style to the whole line) -
    this is why captions moved to .ass in phase 2.

    Real-video review (2026-07-23, run #36) showed captions coming out
    plain white for most chunks in practice - HIGHLIGHT_KEYWORDS is a fixed
    list of emotionally-loaded words, and most 3-4 word caption chunks
    simply don't happen to contain one, so the "colored caption" identity
    almost never actually showed on screen. When force_highlight is True
    (the default) and no HIGHLIGHT_KEYWORDS word matched, fall back to
    coloring the single longest non-stopword in the chunk instead, so
    nearly every caption chunk gets some color pop rather than only the
    rare keyword hit - this is what actually makes it read as a
    consistently "colored caption style" across a whole video.
    """
    matched = False

    def repl(match):
        nonlocal matched
        word = match.group(0)
        core = re.sub(r"[^A-Za-z']", "", word).lower()
        if core in HIGHLIGHT_KEYWORDS:
            matched = True
            return (
                r"{\c&H" + HIGHLIGHT_ASS_COLOR + r"&}"
                + word.upper()
                + r"{\c&H" + BASE_ASS_COLOR + r"&}"
            )
        return word

    result = re.sub(r"\S+", repl, sentence)
    if matched or not force_highlight:
        return result

    words = sentence.split()
    candidates = [
        w for w in words
        if len(re.sub(r"[^A-Za-z']", "", w)) >= 4
        and re.sub(r"[^A-Za-z']", "", w).lower() not in _HIGHLIGHT_FALLBACK_STOPWORDS
    ]
    if not candidates:
        return result
    target = max(candidates, key=lambda w: len(re.sub(r"[^A-Za-z']", "", w)))
    idx = words.index(target)
    words[idx] = (
        r"{\c&H" + HIGHLIGHT_ASS_COLOR + r"&}"
        + target.upper()
        + r"{\c&H" + BASE_ASS_COLOR + r"&}"
    )
    return " ".join(words)

def build_ass(sentences: list, segment_durations: list, dest_path: str) -> None:
    """Build a MindByte-branded .ass caption file.

    Captions are chunked into short word-groups (roughly 3-4 words) that
    appear and disappear in sequence within each sentence's real measured
    TTS duration, instead of holding the full sentence on screen the whole
    time. This reads as a dynamic, premium caption style (matching what
    top-performing psychology Shorts use) rather than a static subtitle
    block. Emotionally-loaded keywords still get the gold highlight via
    _highlight_ass_text().
    """
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {VIDEO_WIDTH}\n"
        f"PlayResY: {VIDEO_HEIGHT}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Caption,Liberation Sans,74,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,3,2,2,60,60,420,1\n"
        "Style: Hook,Liberation Sans,92,&H00FFFFFF,&H000000FF,&H00000000,&H96000000,1,0,0,0,100,100,0,0,1,4,3,2,50,50,420,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    def _fmt_time(seconds: float) -> str:
        seconds = max(0.0, seconds)
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = seconds % 60
        cs = int(round((s - int(s)) * 100))
        if cs >= 100:
            cs = 0
            s += 1
        return f"{h:d}:{m:02d}:{int(s):02d}.{cs:02d}"

    def _chunk_words(words: list, max_words: int = 4) -> list:
        chunks = []
        cur = []
        for w in words:
            cur.append(w)
            if len(cur) >= max_words:
                chunks.append(cur)
                cur = []
        if cur:
            chunks.append(cur)
        return chunks

    events = []
    cursor = 0.0
    is_first_chunk_overall = True
    for sentence, duration in zip(sentences, segment_durations):
        words = sentence.split()
        if not words:
            cursor += duration
            continue
        chunks = _chunk_words(words, max_words=4)
        chunk_lens = [max(1, sum(len(w) for w in c)) for c in chunks]
        total_len = sum(chunk_lens)
        t = cursor
        for chunk, clen in zip(chunks, chunk_lens):
            chunk_dur = duration * (clen / total_len) if total_len else duration / len(chunks)
            # The opening hook chunk gets a bigger, bolder "Hook" style and
            # a slightly longer minimum hold than the rest of the chunked
            # captions, so the curiosity-driving first words land clearly
            # as a real visual hook cue instead of flashing by identically
            # to every other caption chunk (phase-3 review follow-up).
            style_name = "Hook" if is_first_chunk_overall else "Caption"
            min_dur = 0.45 if is_first_chunk_overall else 0.28
            chunk_dur = max(chunk_dur, min_dur)
            start = t
            end = min(t + chunk_dur, cursor + duration)
            text = _highlight_ass_text(" ".join(chunk))
            events.append(
                f"Dialogue: 0,{_fmt_time(start)},{_fmt_time(end)},{style_name},,0,0,0,,{text}"
            )
            t = end
            is_first_chunk_overall = False
        cursor += duration

    with open(dest_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(events))
        f.write("\n")

def assemble_video(clip_paths: list, segment_durations: list, audio_path: str,
                    ass_path: str, output_path: str,
                    title_card_path: str = None, watermark_path: str = None) -> None:
    # Each clip is trimmed to the real measured duration of the sentence it
    # illustrates (see generate_voiceover_segments), so cuts land exactly on
    # sentence boundaries instead of an even, content-blind split. zip()
    # naturally trims to the shorter list if a clip slot was unfilled.
    workdir = os.path.dirname(output_path)
    normalized = []
    for i, (clip, dur) in enumerate(zip(clip_paths, segment_durations)):
        norm_path = os.path.join(workdir, f"norm_{i}.mp4")
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", clip, "-t", f"{dur:.3f}",
                "-vf",
                f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
                f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},fps=30,setsar=1,"
                f"eq=contrast=1.06:saturation=0.9:brightness=0.01,vignette=PI/4",
                "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                "-pix_fmt", "yuv420p",
                norm_path,
            ],
            check=True, capture_output=True,
        )
        normalized.append(norm_path)

    concat_list = os.path.join(workdir, "concat.txt")
    with open(concat_list, "w") as f:
        for p in normalized:
            f.write(f"file '{os.path.abspath(p)}'\n")

    silent_video = os.path.join(workdir, "silent.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list,
         "-c", "copy", silent_video],
        check=True, capture_output=True,
    )

    # The .ass file (built by build_ass()) carries its own Style line -
    # positioning (MarginV=420 etc., see caption-overlap fix history) and
    # per-word highlight colors are both embedded there now, so no
    # force_style override is needed here (content-strategy phase 2,
    # 2026-07-19 - previously this used a plain .srt + force_style, which
    # can't do the per-word highlighting an .ass file's override tags can).
    ass_escaped = ass_path.replace(":", r"\:")

    # Small "Follow MindByte for more" cue burned in for the last ~1.8s of
    # the video, positioned near the TOP of the frame (captions own the
    # bottom) so it never collides with the last line's caption. This is a
    # plain growth/branding cue, not part of the spoken narration, so it
    # doesn't affect compliance_check()'s originality scan.
    total_duration = sum(segment_durations)
    follow_from = max(total_duration - 1.8, 0.0)
    follow_overlay = (
        "drawtext=text='Follow MindByte for more':fontcolor=white:fontsize=54:"
        "font=Arial:box=1:boxcolor=black@0.45:boxborderw=14:"
        f"x=(w-text_w)/2:y=180:enable='gte(t\\,{follow_from:.3f})'"
    )

    # Title card + watermark are added as extra overlay inputs on top of
    # the existing subtitles/follow-cue chain (content-strategy branding
    # pass, 2026-07-19). The title card clip is time-limited to
    # TITLE_CARD_SECONDS with eof_action=pass, so once it ends the
    # overlay simply stops covering the frame - no need to shift the
    # audio/caption timeline at all. The watermark has no time limit and
    # is bounded only by -shortest, so it persists for the whole video.
    # Verified locally with a synthetic render (solid-color test clips +
    # a sine-tone track) before this went live.
    filter_stages = [f"[0:v]subtitles={ass_escaped},{follow_overlay}[capped]"]
    extra_input_args = []
    next_input_index = 2  # 0=silent_video, 1=audio_path
    last_label = "capped"
    if title_card_path:
        extra_input_args += ["-loop", "1", "-t", f"{TITLE_CARD_SECONDS:.2f}", "-i", title_card_path]
        filter_stages.append(
            f"[{last_label}][{next_input_index}:v]overlay=0:0:eof_action=pass[titled]"
        )
        last_label = "titled"
        next_input_index += 1
    if watermark_path:
        extra_input_args += ["-loop", "1", "-i", watermark_path]
        filter_stages.append(
            f"[{last_label}][{next_input_index}:v]overlay=40:40[branded]"
        )
        last_label = "branded"
        next_input_index += 1
    filter_complex = ";".join(filter_stages)

    subprocess.run(
        [
            "ffmpeg", "-y", "-i", silent_video, "-i", audio_path, *extra_input_args,
            "-filter_complex", filter_complex,
            "-map", f"[{last_label}]", "-map", "1:a:0",
            "-c:v", "libx264", "-preset", "medium", "-crf", "17",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-c:a", "aac", "-b:a", "192k", "-shortest",
            output_path,
        ],
        check=True, capture_output=True,
    )

def upload_to_youtube(access_token: str, video_path: str, title: str, description: str,
                       tags: list, publish_at_iso: str) -> str:
    metadata = {
        "snippet": {
            "title": title[:100],
            "description": description,
            "tags": tags,
            "categoryId": "27",  # Education
        },
        "status": {
            "privacyStatus": "private",
            "publishAt": publish_at_iso,
            "selfDeclaredMadeForKids": False,
        },
    }
    init = SESSION.post(
        "https://www.googleapis.com/upload/youtube/v3/videos"
        "?uploadType=resumable&part=snippet,status",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "video/mp4",
        },
        data=json.dumps(metadata),
        timeout=30,
    )
    init.raise_for_status()
    upload_url = init.headers["Location"]

    with open(video_path, "rb") as f:
        video_bytes = f.read()
    upload_resp = SESSION.put(
        upload_url,
        headers={"Content-Type": "video/mp4"},
        data=video_bytes,
        timeout=600,
    )
    upload_resp.raise_for_status()
    return upload_resp.json()["id"]


# ---------------------------------------------------------------------------
# Phase 3: automated pre-publish quality checklist
# ---------------------------------------------------------------------------

FORBIDDEN_HOOK_OPENERS = [
    "welcome back", "today we will discuss", "did you know",
    "in this video", "hey guys", "hey everyone", "what's up guys",
]
MIN_VIDEO_DURATION_SEC = 40
MAX_VIDEO_DURATION_SEC = 80
REQUIRED_WIDTH = 1080
REQUIRED_HEIGHT = 1920
QUALITY_SHEET_TAB = "QualityChecklist!A:N"


def set_youtube_thumbnail(access_token: str, video_id: str, thumbnail_path: str) -> None:
    """Uploads a custom branded thumbnail for the given video via
    YouTube's thumbnails.set endpoint. Called only after the video
    itself has already uploaded successfully - any failure here (a
    transient API error, or custom-thumbnail eligibility not fully
    propagated on the channel yet) is caught by the caller in main()
    and must never be treated as a reason the whole run failed."""
    with open(thumbnail_path, "rb") as f:
        image_bytes = f.read()
    headers = google_headers(access_token)
    headers["Content-Type"] = "image/png"
    resp = SESSION.post(
        "https://www.googleapis.com/upload/youtube/v3/thumbnails/set",
        params={"videoId": video_id},
        headers=headers,
        data=image_bytes,
        timeout=60,
    )
    resp.raise_for_status()


def ffprobe_video_info(path: str) -> dict:
    """Technical sanity-check on the final assembled video: resolution,
    duration, and whether an audio stream actually made it into the file.
    This catches assembly-level breakage (e.g. a silent render, or a frame
    size regression) that script/idea scoring alone can't see."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", path],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(out.stdout)
    duration = float(data.get("format", {}).get("duration", 0))
    width = height = 0
    has_audio = False
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and not width:
            width = stream.get("width", 0)
            height = stream.get("height", 0)
        if stream.get("codec_type") == "audio":
            has_audio = True
    return {"duration": duration, "width": width, "height": height, "has_audio": has_audio}


def run_prepublish_checklist(
    topic: str, pillar: str, script: dict, quality: dict, compliance: dict,
    idea_score_avg: float, video_path: str,
) -> dict:
    """Final automated gate mirroring the content-strategy doc's 'Quality
    Control Before Publishing' checklist. Combines signals already computed
    earlier in the run (idea score, script quality, compliance) with fresh
    technical checks on the actual assembled video file, so a broken render
    can't slip through even if the script itself scored well."""
    checks = {}

    hook = script["sentences"][0].strip().lower() if script.get("sentences") else ""
    checks["hook_ok"] = not any(hook.startswith(p) for p in FORBIDDEN_HOOK_OPENERS)
    checks["idea_score_ok"] = idea_score_avg >= IDEA_SCORE_AVG_THRESHOLD
    checks["script_quality_ok"] = quality["score"] >= QUALITY_THRESHOLD
    checks["compliance_ok"] = compliance["passed"]
    checks["tags_ok"] = 5 <= len(script.get("tags", [])) <= 20

    try:
        info = ffprobe_video_info(video_path)
    except Exception as e:  # noqa: BLE001 - never crash the checklist itself
        info = {"duration": 0, "width": 0, "height": 0, "has_audio": False}
        print(f"[pipeline] checklist: ffprobe failed, treating as fail: {e}")

    checks["duration_ok"] = MIN_VIDEO_DURATION_SEC <= info["duration"] <= MAX_VIDEO_DURATION_SEC
    checks["resolution_ok"] = info["width"] == REQUIRED_WIDTH and info["height"] == REQUIRED_HEIGHT
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


def ensure_sheet_tab(access_token: str, tab_name: str, header_row: list) -> bool:
    """Best-effort self-heal: creates the named Sheet tab (plus a header
    row) if it doesn't exist yet, so a logging function that hits a 400
    doesn't just print the same warning forever - it can fix the gap the
    first time it's hit. Returns True if the tab now exists (freshly
    created), False if creation itself failed, in which case the caller
    falls back to its original warning."""
    try:
        resp = SESSION.post(
            f"{SHEETS_BASE}:batchUpdate",
            headers=google_headers(access_token),
            json={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
            timeout=30,
        )
        if resp.status_code != 200:
            return False
        sheet_append(access_token, f"{tab_name}!A:Z", header_row)
        return True
    except Exception:
        return False

def log_quality_checklist(
    access_token: str, topic: str, pillar: str, result: dict, video_id: str = "",
) -> None:
    """Writes one row per video to the 'QualityChecklist' Sheet tab so
    pre-publish check results are visible at a glance instead of being a
    silent pass/fail buried in the Actions run log."""
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
        ", ".join(result["failed"]) if result["failed"] else "",
    ]
    try:
        sheet_append(access_token, QUALITY_SHEET_TAB, row)
    except Exception as e:  # noqa: BLE001 - logging must never abort the run
        header = [
            "Timestamp", "VideoID", "Topic", "Pillar", "OverallResult",
            "HookOK", "IdeaScoreOK", "ScriptQualityOK", "ComplianceOK",
            "DurationOK", "ResolutionOK", "AudioOK", "TagsOK", "FailedChecks",
        ]
        healed = False
        try:
            healed = ensure_sheet_tab(access_token, "QualityChecklist", header)
            if healed:
                sheet_append(access_token, QUALITY_SHEET_TAB, row)
        except Exception:
            healed = False
        if not healed:
            print(
                f"[pipeline] could not log to QualityChecklist tab (does it exist "
                f"in the Sheet yet?): {e}"
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    access_token = get_access_token()
    topic, pillar, idea_score_avg = pick_topic_with_idea_score(access_token)
    print(f"[pipeline] topic: {topic} (pillar: {pillar}) - idea score avg {idea_score_avg:.1f}")

    script, quality = generate_and_score_script(topic, pillar)
    print(f"[pipeline] title: {script['title']}")
    print(f"[pipeline] final quality score: {quality['score']} - {quality['notes']}")

    compliance = compliance_check(script)
    print(f"[pipeline] compliance: {compliance}")

    created_date = datetime.now(timezone.utc).isoformat()
    sheet_row_base = [
        "", script["title"], topic, "", created_date, "",
        quality["score"], compliance["notes"], 0, 0, 0, 0, "", "", "",
    ]

    if quality["score"] < QUALITY_THRESHOLD or not compliance["passed"]:
        sheet_row_base[3] = "Rejected"
        sheet_row_base[14] = "Skipped upload: failed quality/compliance gate"
        sheet_append(access_token, "Videos!A:O", sheet_row_base)
        print("[pipeline] rejected by quality/compliance gate - no upload")
        return

    with tempfile.TemporaryDirectory() as workdir:
        scene_categories = classify_scene_categories(script.get("sentences") or [])
        print(f"[pipeline] scene categories: {scene_categories}")
        clip_paths, stock_attributions = gather_clips(
            script["visual_keywords"], workdir, sentences=script.get("sentences"),
            scene_categories=scene_categories,
        )
        if not clip_paths:
            sheet_row_base[3] = "Failed"
            sheet_row_base[14] = "No usable stock clips found (Pexels/Pixabay/Coverr)"
            sheet_append(access_token, "Videos!A:O", sheet_row_base)
            print("[pipeline] no clips found - aborting")
            return

        # Each sentence is synthesized as its own TTS clip (not one long
        # combined paragraph) so the delivery has distinct beats instead of
        # a flat, run-on read - see generate_voiceover_segments() docstring.
        # segment_durations here are REAL measured per-sentence durations,
        # not a word-count estimate, so captions/cuts land exactly on them.
        audio_path, segment_durations = generate_voiceover_segments(script["sentences"], workdir, pillar)
        audio_duration = ffprobe_duration(audio_path)

        # Background music is best-effort: fetch + mix under the narration,
        # but fall back to the plain voiceover on any failure rather than
        # aborting the run over a missing music track.
        final_audio_path = audio_path
        description = script["description"]
        for attribution in stock_attributions:
            # Currently only Coverr's free-tier clips carry a required
            # credit line - Pexels/Pixabay clips used here need none.
            if attribution not in description:
                description += f"\n\n{attribution}"
        music_path = os.path.join(workdir, "music.mp3")
        music_meta = fetch_background_music(music_path, pillar)
        if music_meta:
            mixed_path = os.path.join(workdir, "voiceover_mixed.mp3")
            try:
                mix_background_music(audio_path, music_path, audio_duration, mixed_path)
                final_audio_path = mixed_path
                print(f"[pipeline] music: '{music_meta['title']}' by {music_meta['creator']} ({music_meta['license']})")
                if music_meta["license"] != "cc0":
                    description += (
                        f"\n\nMusic: \"{music_meta['title']}\" by "
                        f"{music_meta['creator']} ({music_meta['license'].upper()})"
                    )
            except Exception as e:  # noqa: BLE001 - music mix must never abort the run
                print(f"[pipeline] music mix failed, continuing without music: {e}")

        mastered_audio_path = os.path.join(workdir, "voiceover_mastered.mp3")
        try:
            master_audio(final_audio_path, mastered_audio_path, audio_duration)
            final_audio_path = mastered_audio_path
            print("[pipeline] audio mastering (loudness normalize + light compression) applied")
        except Exception as e:  # noqa: BLE001 - mastering must never abort the run
            print(f"[pipeline] audio mastering failed, continuing with unmastered audio: {e}")

        title_card_path = os.path.join(workdir, "title_card.png")
        watermark_path = os.path.join(workdir, "watermark.png")
        try:
            build_title_card(title_card_path, script["title"], pillar)
            build_watermark_png(watermark_path)
        except Exception as e:  # noqa: BLE001 - branding must never abort the run
            print(f"[pipeline] branding assets failed, continuing without them: {e}")
            title_card_path = None
            watermark_path = None

        thumbnail_path = os.path.join(workdir, "thumbnail.png")
        try:
            build_thumbnail(thumbnail_path, script["title"], pillar)
        except Exception as e:  # noqa: BLE001 - thumbnail must never abort the run
            print(f"[pipeline] thumbnail generation failed, continuing without a custom thumbnail: {e}")
            thumbnail_path = None

        ass_path = os.path.join(workdir, "captions.ass")
        build_ass(script["sentences"], segment_durations, ass_path)

        output_path = os.path.join(workdir, "final.mp4")
        assemble_video(clip_paths, segment_durations, final_audio_path, ass_path, output_path,
                        title_card_path=title_card_path, watermark_path=watermark_path)

        checklist = run_prepublish_checklist(
            topic, pillar, script, quality, compliance, idea_score_avg, output_path,
        )
        print(f"[pipeline] pre-publish checklist: {checklist['checks']}")
        if not checklist["overall_pass"]:
            sheet_row_base[3] = "Failed"
            sheet_row_base[14] = f"Failed pre-publish checklist: {', '.join(checklist['failed'])}"
            sheet_append(access_token, "Videos!A:O", sheet_row_base)
            log_quality_checklist(access_token, topic, pillar, checklist)
            print(f"[pipeline] rejected by pre-publish checklist: {checklist['failed']}")
            return

        publish_at = datetime.now(timezone.utc) + timedelta(hours=PUBLISH_DELAY_HOURS)
        video_id = upload_to_youtube(
            access_token, output_path, script["title"], description,
            script["tags"], publish_at.isoformat(),
        )
        print(f"[pipeline] uploaded video id: {video_id}")

        if thumbnail_path:
            try:
                set_youtube_thumbnail(access_token, video_id, thumbnail_path)
                print("[pipeline] custom branded thumbnail set")
            except Exception as e:  # noqa: BLE001 - thumbnail upload must never abort the run
                print(f"[pipeline] could not set custom thumbnail: {e}")

    sheet_row_base[0] = video_id
    sheet_row_base[3] = "Scheduled"
    sheet_row_base[5] = publish_at.isoformat()
    sheet_row_base[13] = f"https://youtu.be/{video_id}"
    sheet_append(access_token, "Videos!A:O", sheet_row_base)
    log_quality_checklist(access_token, topic, pillar, checklist, video_id)
    mark_topic_used(access_token, topic, video_id)
    print("[pipeline] done")


if __name__ == "__main__":
    main()
