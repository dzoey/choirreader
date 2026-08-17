"""Apply vision-model corrections back into MusicXML.

The vision model returns a list of discrepancy dicts. This module turns those
into concrete edits on the MusicXML document. Not every discrepancy is
machine-applicable (e.g. "the whole phrase is wrong"); those are logged and
left for a human, but the common cases — wrong pitch, flattened chord, wrong
duration, wrong time signature, wrong lyric — are applied directly.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from xml.etree import ElementTree as ET

log = logging.getLogger("choirreader.correction.apply")

# Map discrepancy "type" -> internal handler name.
_TYPE_MAP = {
    "note_pitch": "note_pitch",
    "pitch": "note_pitch",
    "chord": "chord",
    "lyric": "lyric",
    "rhythm": "rhythm",
    "time_signature": "time_signature",
}


def apply_corrections(musicxml_path: Path, discrepancies: list[dict]) -> dict:
    """Apply a list of discrepancies to a MusicXML file (in place).

    Accepts both compressed ``.mxl`` (a ZIP containing a ``.xml``) and raw
    ``.musicxml`` / ``.xml`` files. Returns a summary dict:
    {"applied": int, "skipped": int, "skipped_detail": [...]}.
    """
    tree, root = _load_musicxml(musicxml_path)

    applied = 0
    skipped = []

    for disc in discrepancies:
        typ = _TYPE_MAP.get(disc.get("type", "").lower())
        if typ is None:
            skipped.append(f"unknown type {disc.get('type')!r}")
            continue
        handler = _HANDLERS.get(typ)
        if handler is None:
            skipped.append(f"no handler for {typ}")
            continue
        try:
            if handler(root, disc):
                applied += 1
            else:
                skipped.append(f"{typ}: {disc.get('description', '')[:80]}")
        except Exception as e:  # noqa: BLE001 — keep the loop alive
            skipped.append(f"{typ} error: {e}")

    _save_musicxml(tree, musicxml_path)
    return {"applied": applied, "skipped": len(skipped), "skipped_detail": skipped}


def _load_musicxml(path: Path):
    """Load a MusicXML file (raw or .mxl) and return (tree, root)."""
    if path.suffix.lower() == ".mxl":
        import zipfile

        with zipfile.ZipFile(path) as z:
            xml_name = next(
                n for n in z.namelist()
                if n.endswith(".xml") and "META-INF" not in n
            )
            data = z.read(xml_name)
        root = ET.fromstring(data)
        tree = ET.ElementTree(root)
        return tree, root
    tree = ET.parse(path)
    return tree, tree.getroot()


def _save_musicxml(tree, path: Path) -> None:
    """Write a MusicXML tree back, preserving .mxl (zip) vs raw format."""
    if path.suffix.lower() == ".mxl":
        import zipfile

        xml_bytes = ET.tostring(tree.getroot(), encoding="UTF-8", xml_declaration=True)
        # Rebuild the .mxl: the XML plus a META-INF/container.xml.
        container = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<container version="1.0">\n'
            '  <rootfiles>\n'
            '    <rootfile full-path="score.xml" media-type="application/vnd.recordare.musicxml+xml"/>\n'
            '  </rootfiles>\n'
            '</container>'
        )
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("score.xml", xml_bytes)
            z.writestr("META-INF/container.xml", container)
        return
    tree.write(path, xml_declaration=True, encoding="UTF-8")


# ---------------------------------------------------------------------------
# Handlers. Each returns True if it made a change.
# ---------------------------------------------------------------------------


def _find_measure(root, number):
    """Find a <measure> by its number attribute (string or int)."""
    if number is None:
        return None
    target = str(number)
    for m in root.iter("measure"):
        if m.get("number") == target:
            return m
    return None


def _notes_in_measure(measure):
    return measure.findall("note")


def _set_pitch(note, step, octave, alter=None):
    """Set (or create) the <pitch> child of a note."""
    pitch = note.find("pitch")
    if pitch is None:
        pitch = ET.SubElement(note, "pitch")
    for tag in ("step", "octave", "alter"):
        el = pitch.find(tag)
        if el is not None:
            pitch.remove(el)
    ET.SubElement(pitch, "step").text = step
    ET.SubElement(pitch, "octave").text = str(octave)
    if alter is not None and int(alter) != 0:
        ET.SubElement(pitch, "alter").text = str(alter)


def _note_pitch(root, disc):
    """Change a note's pitch. Requires measure + a way to identify the note."""
    measure = _find_measure(root, disc.get("measure"))
    if measure is None:
        return False
    expected = disc.get("expected", "")
    step, octave, alter = _parse_pitch(expected)
    if step is None:
        return False
    # Identify the note: use "staff" or "note_index" if provided, else first note.
    notes = _notes_in_measure(measure)
    idx = disc.get("note_index", 0)
    if idx >= len(notes):
        return False
    note = notes[idx]
    if note.find("rest") is not None:
        return False
    _set_pitch(note, step, octave, alter)
    return True


def _chord(root, disc):
    """Merge sequential notes at the same position into a chord.

    The vision model reports a flattened chord. We mark the second (and later)
    note at the same default-x as a <chord/> continuation of the first.
    """
    measure = _find_measure(root, disc.get("measure"))
    if measure is None:
        return False
    notes = _notes_in_measure(measure)
    if len(notes) < 2:
        return False
    # Group by default-x (within tolerance) and mark continuations.
    changed = False
    last_x = None
    for note in notes:
        x = note.get("default-x")
        if x is None:
            continue
        try:
            x = int(float(x))
        except (TypeError, ValueError):
            continue
        if last_x is not None and abs(x - last_x) <= 5:
            if note.find("chord") is None and note.find("rest") is None:
                ET.SubElement(note, "chord")
                changed = True
        last_x = x
    return changed


def _lyric(root, disc):
    """Fix a lyric syllable on a note."""
    measure = _find_measure(root, disc.get("measure"))
    if measure is None:
        return False
    expected = disc.get("expected", "")
    if not expected:
        return False
    notes = _notes_in_measure(measure)
    idx = disc.get("note_index", 0)
    if idx >= len(notes):
        return False
    note = notes[idx]
    lyric = note.find("lyric")
    if lyric is None:
        lyric = ET.SubElement(note, "lyric")
    text = lyric.find("text")
    if text is None:
        text = ET.SubElement(lyric, "text")
    text.text = expected
    return True


def _rhythm(root, disc):
    """Change a note's duration/type. Requires expected duration in beats."""
    measure = _find_measure(root, disc.get("measure"))
    if measure is None:
        return False
    expected = disc.get("expected", "")
    dur = _parse_duration(expected)
    if dur is None:
        return False
    notes = _notes_in_measure(measure)
    idx = disc.get("note_index", 0)
    if idx >= len(notes):
        return False
    note = notes[idx]
    d = note.find("duration")
    if d is None:
        d = ET.SubElement(note, "duration")
    d.text = str(dur)
    return True


def _time_signature(root, disc):
    """Change the time signature. Expected like '3/4' or '4/4'."""
    expected = disc.get("expected", "")
    parts = expected.split("/")
    if len(parts) != 2:
        return False
    try:
        beats, beat_type = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    # Apply to the first measure's <attributes> (and optionally all measures).
    first_measure = root.find(".//measure")
    if first_measure is None:
        return False
    attrs = first_measure.find("attributes")
    if attrs is None:
        attrs = ET.SubElement(first_measure, "attributes")
    time = attrs.find("time")
    if time is None:
        time = ET.SubElement(attrs, "time")
    for tag in ("beats", "beat-type"):
        el = time.find(tag)
        if el is not None:
            time.remove(el)
    ET.SubElement(time, "beats").text = str(beats)
    ET.SubElement(time, "beat-type").text = str(beat_type)
    return True


def _parse_pitch(s):
    """Parse 'C4', 'A#4', 'Bb3' -> (step, octave, alter)."""
    if not s:
        return None, None, None
    s = s.strip()
    step = s[0].upper()
    rest = s[1:]
    alter = 0
    if rest and rest[0] in "#♯":
        alter = 1
        rest = rest[1:]
    elif rest and rest[0] in "b♭":
        alter = -1
        rest = rest[1:]
    try:
        octave = int(rest)
    except ValueError:
        return None, None, None
    return step, octave, alter


def _parse_duration(s):
    """Parse a duration string like 'quarter', 'half', 'eighth', or a number."""
    if s is None:
        return None
    s = str(s).strip().lower()
    table = {
        "whole": 4, "half": 2, "quarter": 1, "eighth": 0.5, "16th": 0.25,
        "semibreve": 4, "minim": 2, "crotchet": 1, "quaver": 0.5,
    }
    if s in table:
        return table[s]
    try:
        return float(s)
    except ValueError:
        return None


_HANDLERS = {
    "note_pitch": _note_pitch,
    "chord": _chord,
    "lyric": _lyric,
    "rhythm": _rhythm,
    "time_signature": _time_signature,
}
