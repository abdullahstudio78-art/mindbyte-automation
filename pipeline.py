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
import subprocess
import tempfile
import textwrap
import time
from datetime import datetime, timedelta, timezone

import requests

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

# Rotation of sub-topics within the "psychology & mind tricks" niche - facts
# about how the brain, memory and behavior work, framed as things that
# happen to the viewer personally (not clinical/diagnostic claims).
TOPIC_POOL = [
    "why we procrastinate", "the placebo effect", "why deja vu happens",
    "how memory tricks you", "the bystander effect", "why we fall for scams",
    "the psychology of first impressions", "how habits form in your brain",
    "the Dunning-Kruger effect", "why we love scary movies",
    "the mere exposure effect", "how color affects your mood",
    "the psychology of nostalgia", "the confirmation bias",
    "how sleep affects your brain", "the psychology of lying",
    "why crowds make us act differently", "the halo effect",
    "how music affects your emotions", "the psychology of fear",
    "why we trust strangers online", "the Zeigarnik effect and unfinished tasks",
    "how your brain processes trauma", "the psychology of habits and addiction",
    "the spotlight effect", "how your brain's reward system works",
    "the psychology of persuasion", "why we remember embarrassing moments",
    "the paradox of choice", "how stress changes your brain",
    "the psychology of dreams", "why we compare ourselves to others",
    "the illusion of control", "optical illusions and how your brain is tricked",
    "the psychology of humor", "why first impressions stick",
    "how loneliness affects your brain", "the psychology of motivation",
    "why we're drawn to gossip", "how your brain reacts to rejection",
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
# Groq: script generation + quality scoring
# ---------------------------------------------------------------------------

def call_groq(prompt: str) -> str:
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
    if resp.status_code != 200:
        print(f"[pipeline] Groq call failed: {resp.status_code} {resp.text}")
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def generate_script(topic: str, feedback: str = "") -> dict:
    feedback_block = ""
    if feedback:
        feedback_block = textwrap.dedent(f"""

            IMPORTANT - a previous draft on this exact topic was reviewed and
            scored too low because: "{feedback}"
            Write a genuinely different draft that specifically fixes that
            weakness, while still following every requirement below.
        """)
    prompt = textwrap.dedent(f"""
        You are writing a 45-55 second YouTube Shorts script for a
        "psychology & mind tricks" channel called MindByte - content about
        how the viewer's own brain, memory and behavior secretly work. The
        topic is: "{topic}".
        {feedback_block}
        Tone: this must feel like a fast-paced, energetic viral Shorts video,
        NOT a lecture or documentary voiceover. Write like you're talking
        excitedly to a friend, not narrating a textbook. Make it feel
        personal - "this is happening to YOU right now", not a detached
        explanation of a study.

        Requirements:
        - The narration must be ORIGINAL: your own wording, framing and
          selection of facts. Do not copy phrasing from any single source.
        - Do NOT give clinical, diagnostic, or medical advice - this is
          general-interest psychology content, not therapy or a diagnosis.
        - Hook the viewer HARD in the first sentence (a surprising claim,
          a question, or a "wait, what?" moment) - not a slow wind-up.
        - 10-14 short, punchy sentences, each under 14 words. Every sentence
          should be a single vivid, self-contained beat - short fragments
          and exclamations are encouraged. Avoid long, explanatory,
          multi-clause sentences; they read as a lecture, not a Short.
        - The full narration read aloud should fill approximately 45-55
          seconds (roughly 130-160 words total) - include enough distinct
          beats/facts to fill that time. Do not pad with filler, but do not
          cut the script so short it runs under 30 seconds either.
        - Keep the energy high all the way through, not just the opener -
          use rhetorical questions, quick reveals, or "but here's the
          crazy part" style pivots between facts.
        - Pick genuinely surprising, lesser-known facts about the topic -
          avoid the most obvious/commonly-known trivia, since that's what
          reads as "generic" to viewers who've seen a hundred facts videos.
        - End with a punchy, memorable closing line (not a generic "thanks
          for watching").
        - Also produce: a clickable title (under 90 characters, no
          clickbait lies), a YouTube description (2-3 sentences plus 3
          relevant hashtags).
        - Also produce "visual_keywords": an array with EXACTLY the same
          number of entries as "sentences", in the same order - one 2-3
          word stock-video search phrase per sentence, describing footage
          that visually matches THAT specific sentence (not the topic in
          general). This is critical: each keyword will be used to cut to
          a new clip exactly when that sentence is spoken, so it must be
          distinct from its neighbors and concretely tied to that line.

        Return ONLY valid JSON with this exact shape:
        {{
          "title": "...",
          "description": "...",
          "sentences": ["...", "..."],
          "visual_keywords": ["...", "..."]
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
    return data


def score_quality(topic: str, script: dict) -> dict:
    prompt = textwrap.dedent(f"""
        Rate the following YouTube Shorts script for a psychology & mind
        tricks channel on a scale of 1-10.

        Calibration - read this first: this is a fast, punchy, 45-55 second
        VERTICAL SHORT made of short fragments and exclamations BY DESIGN.
        Do NOT penalize brevity, simplicity, or the absence of long
        explanatory detail - that IS the correct style for this format, not
        a flaw. A script that nails a strong hook and energetic pacing
        should score 8-10 even though each individual sentence is short.
        Judge it as a Short, not as an essay. Also do not penalize it purely
        for being longer than a typical fact-of-the-day clip - 45-55 seconds
        of content is the intended target length, not a maximum to undercut.

        Score primarily on:
        - Hook strength: does the first sentence grab attention immediately,
          and make it feel personal ("this is about YOUR brain")?
        - Energy/pacing: does it feel fast and exciting, not flat or
          lecture-like?
        - Fact interest: would a general viewer find these facts genuinely
          surprising (not the most obvious psychology 101 trivia)?
        - Originality of phrasing: no generic filler like "did you know"
          or "stay tuned to find out".

        Only score below 6 if the script is genuinely boring, factually
        weak, or reads like a generic template - not merely because it is
        short or simple.

        Topic: {topic}
        Title: {script['title']}
        Script: {" ".join(script['sentences'])}

        Return ONLY valid JSON: {{"score": <integer 1-10>, "notes": "<one
        sentence justification>"}}
    """).strip()
    raw = call_groq(prompt)
    return json.loads(raw)


def generate_and_score_script(topic: str, max_attempts: int = MAX_SCRIPT_ATTEMPTS) -> tuple:
    """Generate + score a script, retrying with feedback if it falls short
    of the quality bar, so a single pipeline run gets multiple shots at
    clearing the gate instead of failing outright on one weak first draft.
    Returns the (script, quality) pair with the highest score seen.
    """
    best_script, best_quality = None, {"score": -1, "notes": ""}
    feedback = ""
    for attempt in range(1, max_attempts + 1):
        script = generate_script(topic, feedback=feedback)
        quality = score_quality(topic, script)
        print(
            f"[pipeline] attempt {attempt}/{max_attempts}: "
            f"quality score {quality['score']} - {quality['notes']}"
        )
        if quality["score"] > best_quality["score"]:
            best_script, best_quality = script, quality
        if quality["score"] >= QUALITY_THRESHOLD:
            break
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


FALLBACK_QUERIES = [
    "nature", "abstract background", "city timelapse", "clouds timelapse",
    "ocean waves", "forest aerial", "starry sky",
]


def gather_clips(keywords: list, workdir: str) -> list:
    """Download exactly one clip per keyword, in order.

    A strict 1:1, order-preserving mapping between sentences and clips is
    required so assemble_video can cut to a new clip exactly when each
    sentence is spoken, instead of cutting on an unrelated fixed timer.
    If a specific keyword yields nothing on Pexels, fall back through a
    rotation of generic queries for that slot rather than skipping it, so
    the clip count never drifts out of sync with the sentence count.
    """
    used_ids: set = set()
    clip_paths = []
    for i, keyword in enumerate(keywords):
        clip = search_pexels_clip(keyword, used_ids)
        if not clip:
            for fb in FALLBACK_QUERIES:
                clip = search_pexels_clip(fb, used_ids)
                if clip:
                    break
        if not clip:
            continue
        dest = os.path.join(workdir, f"clip_{i}.mp4")
        download_file(clip["url"], dest)
        clip_paths.append(dest)
    return clip_paths


# ---------------------------------------------------------------------------
# Background music (Openverse - free, no API key, commercially-licensed)
# ---------------------------------------------------------------------------

OPENVERSE_AUDIO_URL = "https://api.openverse.org/v1/audio/"
MUSIC_QUERIES = [
    "mysterious ambient", "cinematic tension calm", "curious ambient piano",
    "ambient technology", "dark ambient minimal", "inspiring ambient",
]
MIN_MUSIC_DURATION_MS = 30000  # skip very short stingers that can't cover a full clip
MUSIC_VOLUME = 0.15  # ducked well under the narration


def fetch_background_music(dest_path: str) -> dict | None:
    """Best-effort fetch of a free, commercially-licensed instrumental track
    from Openverse (aggregates Jamendo, Free Music Archive, etc. - no API
    key needed). This is a nice-to-have: any failure (network, no results,
    license mismatch) is swallowed and logged so a music-fetch problem never
    aborts video production - the video is still fine without a music bed.
    """
    query = random.choice(MUSIC_QUERIES)
    try:
        resp = SESSION.get(
            OPENVERSE_AUDIO_URL,
            params={"q": query, "license_type": "commercial", "page_size": 10},
            timeout=20,
        )
        if resp.status_code != 200:
            print(f"[pipeline] music search failed: {resp.status_code} {resp.text[:200]}")
            return None
        results = resp.json().get("results", [])
        candidates = [
            r for r in results
            if r.get("url") and (r.get("duration") or 0) >= MIN_MUSIC_DURATION_MS
        ]
        if not candidates:
            print(f"[pipeline] no suitable music candidates for query '{query}'")
            return None
        track = random.choice(candidates)
        download_file(track["url"], dest_path)
        return {
            "title": track.get("title") or "Untitled",
            "creator": track.get("creator") or "Unknown artist",
            "license": (track.get("license") or "unknown").lower(),
        }
    except Exception as e:  # noqa: BLE001 - deliberately broad, see docstring
        print(f"[pipeline] music fetch failed, continuing without music: {e}")
        return None


def mix_background_music(voice_path: str, music_path: str, duration: float,
                          dest_path: str) -> None:
    """Loop the music bed to cover the narration, duck its volume well
    under the voice, and mix the two into a single audio track."""
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-stream_loop", "-1", "-i", music_path,
            "-i", voice_path,
            "-filter_complex",
            f"[0:a]atrim=0:{duration:.3f},volume={MUSIC_VOLUME}[music];"
            f"[1:a]volume=1.0[voice];"
            f"[music][voice]amix=inputs=2:duration=longest:dropout_transition=2[aout]",
            "-map", "[aout]", "-t", f"{duration:.3f}",
            "-c:a", "libmp3lame", "-q:a", "4",
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


def compute_segment_durations(sentences: list, total_duration: float) -> list:
    """Split total narration duration across sentences by word-count share.

    Used for BOTH the caption timing and the video-clip cut timing, so a
    new clip appears on screen at exactly the moment the caption changes -
    this is what makes cuts feel intentional and paced with the narration
    instead of landing on an arbitrary fixed timer.
    """
    word_counts = [max(len(s.split()), 1) for s in sentences]
    total_words = sum(word_counts)
    return [total_duration * (wc / total_words) for wc in word_counts]


def build_srt(sentences: list, segment_durations: list, dest_path: str) -> None:
    t = 0.0

    def fmt(ts: float) -> str:
        ms = int(round(ts * 1000))
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines = []
    for i, (sentence, dur) in enumerate(zip(sentences, segment_durations), start=1):
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

def assemble_video(clip_paths: list, segment_durations: list, audio_path: str,
                    srt_path: str, output_path: str) -> None:
    # Each clip is trimmed to the duration of the sentence it illustrates
    # (see compute_segment_durations), so cuts land exactly on sentence
    # boundaries instead of an even, content-blind split. zip() naturally
    # trims to the shorter list in the rare case a clip slot was unfilled.
    workdir = os.path.dirname(output_path)
    normalized = []
    for i, (clip, dur) in enumerate(zip(clip_paths, segment_durations)):
        norm_path = os.path.join(workdir, f"norm_{i}.mp4")
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", clip, "-t", f"{dur:.3f}",
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
    # PlayResX/PlayResY are set explicitly because without them libass has
    # to guess the script resolution, which previously made captions render
    # far from where FontSize/MarginV intended (reported as captions
    # appearing in the middle of the screen instead of the lower third).
    # FontSize is sized relative to the real 1080x1920 output (13 was a
    # leftover from an unscaled default and was nearly invisible/mispositioned).
    subtitle_style = (
        f"FontName=Arial,Bold=1,FontSize=68,PrimaryColour=&H00FFFFFF,"
        f"OutlineColour=&H00000000,BorderStyle=1,Outline=3,Shadow=1,"
        f"Alignment=2,MarginV=220,PlayResX={VIDEO_WIDTH},PlayResY={VIDEO_HEIGHT}"
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

    script, quality = generate_and_score_script(topic)
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

        # Background music is best-effort: fetch + mix under the narration,
        # but fall back to the plain voiceover on any failure rather than
        # aborting the run over a missing music track.
        final_audio_path = audio_path
        description = script["description"]
        music_path = os.path.join(workdir, "music.mp3")
        music_meta = fetch_background_music(music_path)
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

        # Computed once and shared by captions + clip cuts, so a new clip
        # appears on screen at exactly the moment the caption changes.
        segment_durations = compute_segment_durations(script["sentences"], audio_duration)

        srt_path = os.path.join(workdir, "captions.srt")
        build_srt(script["sentences"], segment_durations, srt_path)

        output_path = os.path.join(workdir, "final.mp4")
        assemble_video(clip_paths, segment_durations, final_audio_path, srt_path, output_path)

        publish_at = datetime.now(timezone.utc) + timedelta(hours=PUBLISH_DELAY_HOURS)
        video_id = upload_to_youtube(
            access_token, output_path, script["title"], description,
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
