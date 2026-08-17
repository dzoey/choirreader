"""Vision-model comparison: spot discrepancies between PDF page and MusicXML.

Feeds the original PDF page image and the rendered MusicXML image to a local
Ollama vision model and asks it to report discrepancies, in the user's
priority order: note pitches, chords, lyrics, rhythms, time signatures.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import requests


class VisionError(Exception):
    pass


# Priority order the user specified (most important first).
ERROR_PRIORITY = [
    "note pitches",
    "chords",
    "lyrics",
    "rhythms",
    "time signatures",
]

_SYSTEM_PROMPT = (
    "You are a meticulous music-notation proofreader. You are shown two images: "
    "the ORIGINAL sheet music and a RENDERED version produced by an OMR engine. "
    "Find every place where the rendered version differs from the original. "
    "Pay closest attention, in this order of importance: "
    + ", ".join(ERROR_PRIORITY)
    + ". "
    "Report each discrepancy as a JSON object with fields: "
    '"type" (one of: note_pitch, chord, lyric, rhythm, time_signature), '
    '"measure" (measure number, or null if unknown), '
    '"staff" (staff/voice name or null), '
    '"description" (what is wrong), '
    '"expected" (what it should be). '
    "Return ONLY a JSON array of these objects, no prose."
)


def compare_pages(
    original_png: Path,
    rendered_png: Path,
    model: str,
    ollama_url: str = "http://localhost:11434",
) -> list[dict]:
    """Ask the vision model to diff two page images.

    Returns a list of discrepancy dicts (see _SYSTEM_PROMPT for schema).
    """
    original_b64 = _image_to_b64(original_png)
    rendered_b64 = _image_to_b64(rendered_png)

    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {
                "role": "system",
                "content": _SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": (
                    "ORIGINAL sheet music:\n"
                    f"[image]\n"
                    "RENDERED (OMR output) version:\n"
                    f"[image]\n"
                    "List the discrepancies as a JSON array."
                ),
                "images": [original_b64, rendered_b64],
            },
        ],
    }

    try:
        resp = requests.post(
            f"{ollama_url.rstrip('/')}/api/chat",
            json=payload,
            timeout=600,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise VisionError(f"Ollama request failed: {e}") from e

    data = resp.json()
    content = data.get("message", {}).get("content", "")
    return _parse_json_array(content)


def _image_to_b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _parse_json_array(text: str) -> list[dict]:
    """Extract a JSON array from model output, tolerating prose around it."""
    text = text.strip()
    # Strip markdown code fences if present.
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        text = text.lstrip("json").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to find the first [...] block.
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1 or end <= start:
            raise VisionError(f"Could not parse JSON from model output: {text[:500]!r}")
        data = json.loads(text[start : end + 1])
    if not isinstance(data, list):
        raise VisionError(f"Expected a JSON array, got {type(data).__name__}")
    return data
