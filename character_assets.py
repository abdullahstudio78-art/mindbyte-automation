"""
character_assets.py - illustrated-character B-roll selection.

Additive supplement to the existing Pexels stock-footage path used by
pipeline.py and pipeline_longform.py. NOTHING in this module removes or
disables the Pexels flow: select_character_asset() is meant to be tried
FIRST for a given script beat, and callers must fall back to
search_pexels_clip()/gather_clips() exactly as before whenever this module
returns None (no matching character, no matching asset on disk, or the
manifest/assets_root doesn't exist at all - e.g. in CI before real image
assets have been copied in).

Real character art is the same set of images kept locally on the user's
machine under D:\\MindByte\\Characters\\<Name>\\... - as of the Byte test
run (2026-07-23), Byte's essential-tier assets are ALSO checked directly
into this repo under ./character_assets/ so CI/Actions can use them without
any extra copy step. Other characters (Maya, Alex, future roster) remain
unchecked-in until their own essential tiers are built and committed the
same way. The expected structure, whether checked in or supplied via
ASSETS_ROOT another way (env var, default "./character_assets"):

    <ASSETS_ROOT>/Characters/<folder_name>/<asset_type>/<filename>.jpg

Until those files are actually present at ASSETS_ROOT, select_character_asset()
will simply return None for everything, which is the correct/safe behavior -
callers transparently keep using Pexels.
"""

import os
import json
import re

DEFAULT_ASSETS_ROOT = os.environ.get("ASSETS_ROOT", "./character_assets")

ASSET_TYPES = ["Expressions", "Poses", "Scenes", "Reference"]

# Emotional/tone keywords we try to match against filename components, e.g.
# "alex_bedroom_night_phone_anxious.jpg" -> tone "anxious". Order matters
# only in that the first matching tone found in the filename wins; this is
# a simple heuristic, not NLP, by design (per task scope).
TONE_KEYWORDS = [
    "anxious", "anxiety", "worried", "worry",
    "laughing", "laugh", "happy", "joyful", "smiling", "smile",
    "sad", "crying", "heartbroken",
    "angry", "frustrated",
    "confident", "calm", "relaxed",
    "confused", "thinking", "overthinking",
    "surprised", "shocked",
    "neutral",
    "scene",
]


def load_characters_manifest(manifest_path: str = None) -> list:
    """Load and return the list of character dicts from characters.json.

    Returns an empty list (never raises) if the manifest is missing or
    malformed, so callers can safely fall back to Pexels-only behavior.
    """
    if manifest_path is None:
        manifest_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "characters.json")
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("characters", [])
    except (OSError, json.JSONDecodeError):
        return []


def _pick_character(script_beat_text: str, characters_manifest: list) -> dict | None:
    """Pick the character (manifest entry) whose personality_keywords best
    match script_beat_text, via simple case-insensitive substring counting.
    Returns None if no character has any keyword hit (i.e. this beat is
    probably generic and better served by Pexels B-roll)."""
    if not script_beat_text or not characters_manifest:
        return None
    lowered = script_beat_text.lower()
    best_character = None
    best_score = 0
    for character in characters_manifest:
        keywords = character.get("personality_keywords", [])
        score = sum(1 for kw in keywords if kw.lower() in lowered)
        if score > best_score:
            best_score = score
            best_character = character
    return best_character if best_score > 0 else None


def _list_asset_files(assets_root: str, folder_name: str) -> list:
    """Return a flat list of (asset_type, full_path, filename) for every
    image file found on disk under
    <assets_root>/Characters/<folder_name>/<asset_type>/*.
    Silently returns [] if the character's folder doesn't exist yet."""
    results = []
    char_dir = os.path.join(assets_root, "Characters", folder_name)
    if not os.path.isdir(char_dir):
        return results
    for asset_type in ASSET_TYPES:
        type_dir = os.path.join(char_dir, asset_type)
        if not os.path.isdir(type_dir):
            continue
        for filename in sorted(os.listdir(type_dir)):
            if filename.lower().endswith((".jpg", ".jpeg", ".png")):
                results.append((asset_type, os.path.join(type_dir, filename), filename))
    return results


def _detect_tone(script_beat_text: str) -> str | None:
    """Very simple keyword match against the beat text to guess the desired
    emotional tone for the image (e.g. "anxious", "laughing", "neutral")."""
    if not script_beat_text:
        return None
    lowered = script_beat_text.lower()
    for tone in TONE_KEYWORDS:
        if tone in lowered:
            return tone
    return None


def _pick_asset_file(assets_root: str, folder_name: str, script_beat_text: str,
                      exclude_files: set = None) -> tuple | None:
    """Pick the best-matching asset file for a character given the beat text.

    Preference order:
      1. A file whose name contains a detected tone keyword (skipped if
         already used this run, per exclude_files, unless it's the only
         match available).
      2. A rotating pick from Expressions+Poses that hasn't been used yet
         this run, so a video with several untagged/generic Byte beats gets
         visible variety instead of the same still repeating - this was a
         real bug reported after the first test video: nearly every beat
         fell through to the same "neutral" default and the video visibly
         repeated one photo over and over.
      3. Only once every candidate has been used at least once, reuse is
         allowed again (better than falling back to Pexels or erroring).
    Returns (asset_type, full_path, filename) or None if the character has
    no asset files on disk at all.
    """
    files = _list_asset_files(assets_root, folder_name)
    if not files:
        return None
    exclude_files = exclude_files or set()

    tone = _detect_tone(script_beat_text)
    if tone and tone != "scene":
        tone_matches = [f for f in files if tone in f[2].lower()]
        if tone_matches:
            fresh = [f for f in tone_matches if f[2] not in exclude_files]
            return (fresh or tone_matches)[0]

    if tone == "scene":
        scene_matches = [f for f in files if f[0] == "Scenes"]
        if scene_matches:
            fresh = [f for f in scene_matches if f[2] not in exclude_files]
            return (fresh or scene_matches)[0]

    # No tone match - rotate through Expressions+Poses (talking-head/body
    # shots suitable for a generic line) rather than always landing on the
    # same neutral default. Reference/Scenes are excluded from this
    # generic pool: Reference shots are turnaround/model-sheet material,
    # not natural mid-sentence cutaways, and Scenes are handled above.
    generic_pool = [f for f in files if f[0] in ("Expressions", "Poses")] or files
    fresh = [f for f in generic_pool if f[2] not in exclude_files]
    if fresh:
        return fresh[0]

    # Every candidate has been used at least once this run - cycle back to
    # reuse rather than erroring or silently falling back to Pexels for the
    # rest of the video.
    return generic_pool[0]


def select_character_asset(script_beat_text: str, characters_manifest: list,
                            assets_root: str = None, exclude_files: set = None) -> dict | None:
    """Select a character illustration for one beat of script text.

    Args:
        script_beat_text: the sentence/paragraph text for this B-roll slot.
        characters_manifest: list of character dicts, as returned by
            load_characters_manifest().
        assets_root: root directory containing Characters/<name>/... .
            Defaults to ASSETS_ROOT env var / "./character_assets".
        exclude_files: set of bare filenames already used earlier in this
            same video, so consecutive/nearby Byte beats don't repeat the
            same still. Caller should add the returned "filename" to this
            set after each call. Optional - omitting it just disables the
            rotation (falls back to the tone-only match).

    Returns:
        None if no character matches, or the matched character has no
        asset files on disk (assets not present yet - caller should fall
        back to search_pexels_clip()/gather_clips() as usual).
        Otherwise a dict:
            {
                "character": <character name>,
                "asset_type": "Expressions" | "Poses" | "Scenes" | "Reference",
                "filename": <bare filename>,
                "path": <resolved absolute/relative file path>,
            }
    """
    if assets_root is None:
        assets_root = DEFAULT_ASSETS_ROOT

    character = _pick_character(script_beat_text, characters_manifest)
    if character is None:
        return None

    picked = _pick_asset_file(
        assets_root, character.get("folder_name", character.get("name")),
        script_beat_text, exclude_files=exclude_files,
    )
    if picked is None:
        return None

    asset_type, full_path, filename = picked
    if not os.path.isfile(full_path):
        return None

    return {
        "character": character.get("name"),
        "asset_type": asset_type,
        "filename": filename,
        "path": full_path,
    }
