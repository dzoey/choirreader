"""Rhythm normalization: force every measure to its time-signature duration.

Audiveris frequently misreads note durations, producing measures whose voices
sum to more or less than the time signature (e.g. 4 quarter notes in a 3/4
bar). Because each voice advances by a different amount per measure, the
treble and bass staves drift out of alignment over the course of a piece.

The barline is a hard boundary: whatever the OMR read, a measure MUST end at
its time signature. This module quantizes each measure to that boundary:

- Overfull voice: notes that start at/after the barline are dropped; a note
  that straddles the barline has its duration truncated to fit.
- Underfull voice: a rest is appended to fill the remaining time.

It also recomputes the <type> field to match any corrected <duration>, because
music21 and lilypond trust <type> over <duration> and would otherwise read the
wrong length.

This fixes *alignment* (the drift), not wrong note values — those are the
vision-model correction loop's job.
"""

from __future__ import annotations

import logging
from pathlib import Path
from xml.etree import ElementTree as ET

log = logging.getLogger("choirreader.correction.rhythm")

# duration (in divisions) -> (type, dotted). Assumes divisions = quarter note.
# We scale by divisions at call time, so this table is in "quarter units".
_TYPE_TABLE = [
    (4.0, "whole", False),
    (3.0, "half", True),      # dotted half
    (2.0, "half", False),
    (1.5, "quarter", True),   # dotted quarter
    (1.0, "quarter", False),
    (0.75, "eighth", True),   # dotted eighth
    (0.5, "eighth", False),
    (0.25, "16th", False),
    (0.125, "32nd", False),
]


def normalize(musicxml_path: Path) -> dict:
    """Normalize measure durations in place. Returns a summary dict."""
    tree, root = _load(musicxml_path)

    divisions, beats, beat_type = _time_signature(root)
    if divisions is None or beats is None:
        log.warning("No time signature found; skipping rhythm normalization")
        return {"measures_fixed": 0, "notes_trimmed": 0, "rests_added": 0}

    expected = beats * divisions  # total divisions per measure

    summary = {"measures_fixed": 0, "notes_trimmed": 0, "rests_added": 0, "types_fixed": 0}

    for part in root.findall(".//part"):
        for measure in part.findall("measure"):
            _normalize_measure(measure, expected, divisions, summary)
            # Recompute <type> from <duration> for every note: Audiveris emits
            # inconsistent type/duration pairs, and music21/lilypond trust
            # <type>, so a stale <type> yields the wrong length even when the
            # measure's total duration is correct.
            for note in measure.findall("note"):
                d = note.find("duration")
                if d is None or note.find("rest") is not None:
                    continue
                if _fix_type(note, float(d.text), divisions):
                    summary["types_fixed"] += 1

    _save(tree, musicxml_path)
    return summary


def _time_signature(root):
    """Return (divisions, beats, beat_type) from the first measure that has them."""
    for measure in root.findall(".//measure"):
        attrs = measure.find("attributes")
        if attrs is None:
            continue
        div = attrs.find("divisions")
        time = attrs.find("time")
        if div is not None and time is not None:
            beats = time.find("beats")
            beat_type = time.find("beat-type")
            if beats is not None and beat_type is not None:
                return (
                    int(div.text),
                    int(beats.text),
                    int(beat_type.text),
                )
    return None, None, None


def _normalize_measure(measure, expected: int, divisions: int, summary: dict) -> bool:
    """Normalize one measure's voices to `expected` divisions. Returns True if changed.

    Handles chords: a note with a <chord/> child is part of the same chord as
    the preceding note, so it does not advance the time cursor and its duration
    must match the chord's anchor note.
    """
    # First pass: force every chord note's duration to match its anchor, and
    # collect per-voice note lists (chord notes share the anchor's onset).
    voices: dict[str, list[tuple[float, ET.Element]]] = {}
    cur_time = 0.0
    prev_note = None  # the anchor note of the current chord (or None)
    for child in measure:
        tag = child.tag
        if tag == "note":
            v = child.find("voice")
            d = child.find("duration")
            dur = float(d.text) if d is not None else 0.0
            vn = v.text if v is not None else "1"
            is_chord = child.find("chord") is not None
            if is_chord and prev_note is not None:
                # Chord note: same onset as anchor, same duration as anchor.
                anchor_d = prev_note.find("duration")
                anchor_dur = float(anchor_d.text) if anchor_d is not None else dur
                if abs(dur - anchor_dur) > 0.01:
                    d.text = str(int(round(anchor_dur)))
                    _fix_type(child, anchor_dur, divisions)
                    summary["notes_trimmed"] += 1
                voices.setdefault(vn, []).append((cur_time - dur, child))
                # do not advance cur_time for chord notes
            else:
                voices.setdefault(vn, []).append((cur_time, child))
                cur_time += dur
                prev_note = child
        elif tag == "backup":
            d = child.find("duration")
            cur_time -= float(d.text) if d is not None else 0.0
            prev_note = None
        elif tag == "forward":
            d = child.find("duration")
            cur_time += float(d.text) if d is not None else 0.0
            prev_note = None

    changed = False
    for vn, notes in voices.items():
        if not notes:
            continue
        # Total duration: sum of anchor notes only (chord notes share onset).
        # Since chord notes were forced to the anchor's duration, summing all
        # would double-count. Instead, sum distinct onsets' durations.
        total = 0.0
        seen_onsets = set()
        for onset, note in notes:
            d = note.find("duration")
            dur = float(d.text) if d is not None else 0.0
            key = round(onset, 3)
            if key in seen_onsets:
                continue  # chord note, already counted via anchor
            seen_onsets.add(key)
            total += dur

        if abs(total - expected) < 0.01:
            continue  # already correct

        if total > expected:
            # Overfull: drop notes at/after the barline, truncate straddlers.
            for onset, note in notes:
                d = note.find("duration")
                dur = float(d.text) if d is not None else 0.0
                if onset >= expected - 0.01:
                    measure.remove(note)
                    summary["notes_trimmed"] += 1
                    changed = True
                    continue
                if onset + dur > expected + 0.01:
                    new_dur = expected - onset
                    d.text = str(int(round(new_dur)))
                    _fix_type(note, new_dur, divisions)
                    summary["notes_trimmed"] += 1
                    changed = True
        else:
            # Underfull: append a rest to fill the gap.
            gap = expected - total
            last_note = notes[-1][1]
            rest = _make_rest(gap, last_note)
            idx = list(measure).index(last_note)
            measure.insert(idx + 1, rest)
            summary["rests_added"] += 1
            changed = True

    if changed:
        summary["measures_fixed"] += 1
    return changed


def _fix_type(note: ET.Element, duration: float, divisions: int) -> bool:
    """Recompute <type> (and <dot>) to match a <duration>. Returns True if changed.

    music21 and lilypond trust <type> over <duration>, so a stale <type> after
    fixing <duration> would still be read as the wrong length.
    """
    type_el = note.find("type")
    if type_el is None:
        return False
    # Remove existing <dot> children (we recompute dottedness).
    for dot in note.findall("dot"):
        note.remove(dot)

    # Convert duration (in divisions) to quarter-note units.
    quarters = duration / divisions
    for q, type_name, dotted in _TYPE_TABLE:
        if abs(quarters - q) < 0.01:
            changed = type_el.text != type_name
            type_el.text = type_name
            if dotted:
                ET.SubElement(note, "dot")
            return changed
    # Fallback: leave type as-is (unusual duration we can't map cleanly).
    return False


def _make_rest(duration: float, template_note: ET.Element) -> ET.Element:
    """Create a <note> rest element with the given duration, copying voice."""
    rest = ET.Element("note")
    ET.SubElement(rest, "rest")
    ET.SubElement(rest, "duration").text = str(int(round(duration)))
    v = template_note.find("voice")
    if v is not None:
        ET.SubElement(rest, "voice").text = v.text
    return rest


def _load(path: Path):
    if path.suffix.lower() == ".mxl":
        import zipfile

        with zipfile.ZipFile(path) as z:
            xml_name = next(
                n for n in z.namelist()
                if n.endswith(".xml") and "META-INF" not in n
            )
            data = z.read(xml_name)
        root = ET.fromstring(data)
        return ET.ElementTree(root), root
    tree = ET.parse(path)
    return tree, tree.getroot()


def _save(tree, path: Path) -> None:
    if path.suffix.lower() == ".mxl":
        import zipfile

        xml_bytes = ET.tostring(tree.getroot(), encoding="UTF-8", xml_declaration=True)
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
