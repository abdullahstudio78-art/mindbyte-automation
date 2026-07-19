"""
MindByte Automation - main content pipeline.

Runs end-to-end: pick a topic, generate a script with Gemini, source B-roll
from Pexels, synthesize a voiceover, assemble the video with ffmpeg, score
quality, run a compliance check, upload/schedule to YouTube, and log
everything to the Google Sheet dashboard.

All secrets are read from environment variables (populated from GitHub
Actions Secrets in CI).
"""

import os
import io
import json
import random
import subprocess
import tempfile
import textwrap
import time
from datetime import datetime, timedelta, timezone

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
PEXELS_API_KEY = os.environ["PEXELS_API_KEY"]
OAUTH_CLIENT_ID = os.environ["OAUTH_CLIENT_ID"]
OAUTH_CLIENT_SECRET = os.environ["OAUTH_CLIENT_SECRET"]
OAUTH_REFRESH_TOKEN = os.environ["OAUTH_REFRESH_TOKEN"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]

GEMINI_MODEL = "gemini-3.5-flash"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
)

PUBLISH_DELAY_HOURS = 18  # rolling safety delay before a video goes public
TARGET_CLIP_COUNT = 5
VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920

# Rotation of sub-topics within the "bite-sized facts/trivia" niche.
TOPIC_POOL = [
    "deep sea creatures", "ancient Rome", "space exploration", "the human brain",
    "extinct animals", "world's smallest countries", "famous inventions by accident",
    "the Great Wall of China", "volcanoes", "ancient Egypt", "the human body",
    "weird laws around the world", "the Amazon rainforest", "black holes",
    "the history of chocolate", "unusual animal abilities", "the Titanic",
    "ancient Greek myths", "the Sahara desert", "record-breaking buildings",
    "the history of money", "polar animals", "the moon landing", "coral reefs",
    "medieval castles", "the history of writing", "extreme weather",
    "famous shipwrecks", "the solar system's planets", "camouflage in nature",
    "ancient wonders of the world", "the history of the internet",
    "bioluminescent creatures", "desert survival adaptations", "lost cities",
    "the history of flight", "unusual food origins", "glaciers and ice ages",
    "the human senses", "migratory animals", "underground cities",
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

def pick_topic(access_token: str) -> str:
    rows = sheet_get(access_token, "UsedTopics!A2:A")
    used = {r[0].strip().lower() for r in rows if r}
    available = [t for t in TOPIC_POOL if t.lower() not in used]
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


# ---------------------------------------------------------------------------
# Gemini: script generation + quality scoring
# ---------------------------------------------------------------------------

def call_gemini(prompt: str) -> str:
    resp = SESSION.post(
        GEMINI_URL,
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.9, "responseMimeType": "application/json"},
        },
        timeout=60,
    )
    if resp.status_code != 200:
        print(f"[pipeline] Gemini call failed: {resp.status_code} {resp.text}")
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


def generate_script(topic: str) -> dict:
    prompt = textwrap.dedent(f"""
        You are writing a 45-55 second YouTube Shorts script for a "bite-sized
        facts/trivia" channel called MindByte. The topic is: "{topic}".

        Requirements:
        - The narration must be ORIGINAL: your own wording, framing and
          selection of facts. Do not copy phrasing from any single source.
        - Hook the viewer in the first sentence.
        - 5-7 short, punchy sentences suitable for on-screen captions.
        - End with a memorable closing line (not a generic "thanks for
          watching").
        - Also produce: a clickable title (under 90 characters, no
          clickbait lies), a YouTube description (2-3 sentences plus 3
          relevant hashtags), and 6 short search keywords (2-3 words each)
          that describe visuals that would pair well with each sentence,
          suitable for searching a stock video library.

        Return ONLY valid JSON with this exact shape:
        {{
          "title": "...",
          "description": "...",
          "sentences": ["...", "..."],
          "visual_keywords": ["...", "..."]
        }}
    """).strip()
    raw = call_gemini(prompt)
    return json.loads(raw)


def score_quality(topic: str, script: dict) -> dict:
    prompt = textwrap.dedent(f"""
        Rate the following YouTube Shorts script for a facts/trivia channel
        on a scale of 1-10 against these criteria: hook strength in the
        first sentence, pacing/conciseness, factual interest, originality
        of phrasing, and how well it fits a 45-55 second short. Also flag
        if it reads as generic/templated rather than a genuinely distinct
        piece of writing.

        Topic: {topic}
        Title: {script['title']}
        Script: {" ".join(script['sentences'])}

        Return ONLY valid JSON: {{"score": <integer 1-10>, "notes": "<one
        sentence justification>"}}
    """).strip()
    raw = call_gemini(prompt)
    return json.loads(raw)


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
        params={"query": query, "orientation": "portrait", "per_page": 5},
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


def gather_clips(keywords: list, workdir: str) -> list:
    used_ids: set = set()
    clip_paths = []
    queries = list(keywords) + ["nature", "abstract background", "city timelapse"]
    for i, query in enumerate(queries):
        if len(clip_paths) >= TARGET_CLIP_COUNT:
            break
        clip = search_pexels_clip(query, used_ids)
        if not clip:
            continue
        dest = os.path.join(workdir, f"clip_{i}.mp4")
        download_file(clip["url"], dest)
        clip_paths.append(dest)
    return clip_paths


# ---------------------------------------------------------------------------
# Voiceover (edge-tts) + captions
# ---------------------------------------------------------------------------

async def _synthesize(text: str, dest_path: str, voice: str = "en-US-AriaNeural") -> None:
    import edge_tts
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(dest_path)


def generate_voiceover(sentences: list, dest_path: str) -> None:
    import asyncio
    full_text = " ".join(sentences)
    asyncio.run(_synthesize(full_text, dest_path))


def ffprobe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def build_srt(sentences: list, total_duration: float, dest_path: str) -> None:
    word_counts = [max(len(s.split()), 1) for s in sentences]
    total_words = sum(word_counts)
    t = 0.0

    def fmt(ts: float) -> str:
        ms = int(round(ts * 1000))
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines = []
    for i, (sentence, wc) in enumerate(zip(sentences, word_counts), start=1):
        dur = total_duration * (wc / total_words)
        start, end = t, t + dur
        t = end
        lines.append(str(i))
        lines.append(f"{fmt(start)} --> {fmt(end)}")
        lines.append(sentence.strip())
        lines.append("")
    with open(dest_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ---------------------------------------------------------------------------
# ffmpeg assembly
# ---------------------------------------------------------------------------

def assemble_video(clip_paths: list, audio_path: str, srt_path: str, output_path: str) -> None:
    audio_duration = ffprobe_duration(audio_path)
    per_clip = audio_duration / len(clip_paths)

    workdir = os.path.dirname(output_path)
    normalized = []
    for i, clip in enumerate(clip_paths):
        norm_path = os.path.join(workdir, f"norm_{i}.mp4")
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", clip, "-t", f"{per_clip:.3f}",
                "-vf",
                f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
                f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},fps=30,setsar=1",
                "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
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

    srt_escaped = srt_path.replace(":", r"\:")
    subtitle_style = (
        "FontName=Arial,FontSize=13,PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,BorderStyle=1,Outline=2,"
        "Alignment=2,MarginV=120"
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", silent_video, "-i", audio_path,
            "-vf", f"subtitles={srt_escaped}:force_style='{subtitle_style}'",
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k", "-shortest",
            output_path,
        ],
        check=True, capture_output=True,
    )


# ---------------------------------------------------------------------------
# YouTube upload
# ---------------------------------------------------------------------------

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
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    access_token = get_access_token()
    topic = pick_topic(access_token)
    print(f"[pipeline] topic: {topic}")

    script = generate_script(topic)
    print(f"[pipeline] title: {script['title']}")

    quality = score_quality(topic, script)
    print(f"[pipeline] quality score: {quality['score']} - {quality['notes']}")

    compliance = compliance_check(script)
    print(f"[pipeline] compliance: {compliance}")

    created_date = datetime.now(timezone.utc).isoformat()
    sheet_row_base = [
        "", script["title"], topic, "", created_date, "",
        quality["score"], compliance["notes"], 0, 0, 0, 0, "", "", "",
    ]

    if quality["score"] < 6 or not compliance["passed"]:
        sheet_row_base[3] = "Rejected"
        sheet_row_base[14] = "Skipped upload: failed quality/compliance gate"
        sheet_append(access_token, "Videos!A:O", sheet_row_base)
        print("[pipeline] rejected by quality/compliance gate - no upload")
        return

    with tempfile.TemporaryDirectory() as workdir:
        clip_paths = gather_clips(script["visual_keywords"], workdir)
        if not clip_paths:
            sheet_row_base[3] = "Failed"
            sheet_row_base[14] = "No usable Pexels clips found"
            sheet_append(access_token, "Videos!A:O", sheet_row_base)
            print("[pipeline] no clips found - aborting")
            return

        audio_path = os.path.join(workdir, "voiceover.mp3")
        generate_voiceover(script["sentences"], audio_path)
        audio_duration = ffprobe_duration(audio_path)

        srt_path = os.path.join(workdir, "captions.srt")
        build_srt(script["sentences"], audio_duration, srt_path)

        output_path = os.path.join(workdir, "final.mp4")
        assemble_video(clip_paths, audio_path, srt_path, output_path)

        publish_at = datetime.now(timezone.utc) + timedelta(hours=PUBLISH_DELAY_HOURS)
        video_id = upload_to_youtube(
            access_token, output_path, script["title"], script["description"],
            script["visual_keywords"], publish_at.isoformat(),
        )
        print(f"[pipeline] uploaded video id: {video_id}")

    sheet_row_base[0] = video_id
    sheet_row_base[3] = "Scheduled"
    sheet_row_base[5] = publish_at.isoformat()
    sheet_row_base[13] = f"https://youtu.be/{video_id}"
    sheet_append(access_token, "Videos!A:O", sheet_row_base)
    mark_topic_used(access_token, topic, video_id)
    print("[pipeline] done")


if __name__ == "__main__":
    main()
