"""
no_human_filter.py - reject any stock clip that visibly contains a real
human, per the user's explicit direction (2026-07-23): the channel's ONLY
"human" presence should be Byte (the illustrated character) - real people
in stock footage visually clash with his anime style and must never appear,
full stop, not just "avoid where possible."

Why this exists: pipeline.py originally just asked the script-generation
LLM and the FALLBACK_QUERIES list to request "people-free" B-roll. Verified
in run #39 that this is NOT reliable - a shooting-range person, a hand
pressing buttons, a hand with a lighter, and a person at a campfire all
slipped through on a single video, because a query like "campfire warmth"
or "fear response trigger" can still legitimately return a stock clip that
happens to have a person in it, regardless of how the query was worded.
Wording the request differently cannot guarantee the result - only
inspecting the actual downloaded footage can.

Detection approach: OpenCV's built-in Haar cascade (frontal + profile face)
catches faces at various angles, and the built-in HOG people detector
catches full-body figures/silhouettes (the shooting-range and campfire
cases) even when no face is clearly visible. This is a lightweight,
model-free (no extra downloads/API keys needed - both ship inside
opencv-python-headless) two-pass check, not perfect (a hand alone with no
arm/body in frame, e.g. the "pressing a button" or "holding a lighter"
shots, can still slip past both detectors), but it catches the large
majority of real-person clips - much stronger than prompt wording alone.

Usage: gather_clips() downloads a CANDIDATE clip to a temp path, calls
clip_contains_person() on it, and only keeps the candidate if it returns
False - otherwise the temp file is deleted and the next candidate (next
stock result, then next fallback query) is tried instead.
"""

import os

try:
    import cv2
    _CV2_AVAILABLE = True
except ImportError:  # pragma: no cover - only if opencv isn't installed
    _CV2_AVAILABLE = False

_FACE_CASCADES = None
_HOG = None


def _load_detectors():
    """Lazily load the cascades/detector once per process (each is a real
    file-parse/model-load, not free) rather than per-clip."""
    global _FACE_CASCADES, _HOG
    if _FACE_CASCADES is not None:
        return
    cascade_dir = cv2.data.haarcascades
    _FACE_CASCADES = []
    for name in ("haarcascade_frontalface_default.xml", "haarcascade_profileface.xml"):
        path = os.path.join(cascade_dir, name)
        if os.path.isfile(path):
            clf = cv2.CascadeClassifier(path)
            if not clf.empty():
                _FACE_CASCADES.append(clf)
    _HOG = cv2.HOGDescriptor()
    _HOG.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())


def clip_contains_person(video_path: str, sample_frames: int = 5) -> bool:
    """Sample a handful of evenly-spaced frames from video_path and return
    True the moment ANY frame trips either the face cascades or the HOG
    full-body people detector. Returns False (clip looks clean) if no
    sampled frame trips either detector, or True (fail closed / treat as
    "has a person") on any read/decode error - a clip we can't even inspect
    should never be assumed safe.
    """
    if not _CV2_AVAILABLE:
        # No way to check - fail closed. This should only happen if
        # opencv-python-headless isn't installed (missing from requirements),
        # which should never ship silently since it's now a hard dependency.
        return True
    try:
        _load_detectors()
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            cap.release()
            return True
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total_frames <= 0:
            cap.release()
            return True
        sample_frames = max(1, sample_frames)
        indices = [
            int(total_frames * (i + 1) / (sample_frames + 1))
            for i in range(sample_frames)
        ]
        found_person = False
        for idx in indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray_small = cv2.resize(gray, (0, 0), fx=0.5, fy=0.5)
            for cascade in _FACE_CASCADES:
                faces = cascade.detectMultiScale(gray_small, scaleFactor=1.1, minNeighbors=5)
                if len(faces) > 0:
                    found_person = True
                    break
            if found_person:
                break
            # HOG works on a moderate-resolution BGR frame; oversized 1080p
            # source frames are slow and unnecessary for detection.
            h, w = frame.shape[:2]
            scale = 480.0 / max(h, w) if max(h, w) > 480 else 1.0
            frame_small = cv2.resize(frame, (int(w * scale), int(h * scale))) if scale != 1.0 else frame
            rects, _ = _HOG.detectMultiScale(frame_small, winStride=(8, 8), padding=(8, 8), scale=1.05)
            if len(rects) > 0:
                found_person = True
                break
        cap.release()
        return found_person
    except Exception:  # noqa: BLE001 - any decode/detector error, fail closed
        return True
