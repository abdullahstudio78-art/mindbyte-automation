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


def _pick_asset_file(assets_root: str, folder_name: str, script_beat_text: str) -> tuple | None:
    """Pick the best-matching asset file for a character given the beat text.

    Preference order:
      1. A file whose name contains a detected tone keyword.
      2. Any file at all (first available), so a character with assets but
         no tone match still gets *something* rather than falling through
         to Pexels unnecessarily.
    Returns (asset_type, full_path, filename) or None if the character has
    no asset files on disk at all.
    """
    files = _list_asset_files(assets_root, folder_name)
    if not files:
        return None

    tone = _detect_tone(script_beat_text)
    if tone:
        for asset_type, full_path, filename in files:
            if tone in filename.lower():
                return asset_type, full_path, filename

    # No tone match (or no tone detected) - fall back to any available
    # asset, preferring "Scenes" if the beat text hints at a scene, then
    # a neutral-looking default, then "Expressions"/"Poses" as a last
    # resort. Falling straight to files[0] (alphabetically first) was a
    # real bug in practice: for Byte that happened to be an "angry"
    # expression, so every untagged sentence silently got an angry face -
    # a visibly wrong default for a calm/neutral line.
    if tone == "scene":
        for asset_type, full_path, filename in files:
            if asset_type == "Scenes":
                return asset_type, full_path, filename

    for asset_type, full_path, filename in files:
        if "neutral" in filename.lower():
            return asset_type, full_path, filename

    return files[0]


def select_character_asset(script_beat_text: str, characters_manifest: list,
                            assets_root: str = None) -> dict | None:
    """Select a character illustration for one beat of script text.

    Args:
        script_beat_text: the sentence/paragraph text for this B-roll slot.
        characters_manifest: list of character dicts, as returned by
            load_characters_manifest().
        assets_root: root directory containing Characters/<name>/... .
            Defaults to ASSETS_ROOT env var / "./character_assets".

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

    picked = _pick_asset_file(assets_root, character.get("folder_name", character.get("name")), script_beat_text)
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
