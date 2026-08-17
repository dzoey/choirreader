"""Recalculate pitches from .omr y-coordinates.

Audiveris has a systematic bug: for multi-notehead chords (chords with 2+ noteheads
on one stem), it assigns pitches in REVERSE order — the visually HIGHER notehead
gets the LOWER pitch value, and vice versa. Single-notehead notes are correct.

This module reads the .omr file and uses the actual y-coordinates of noteheads
(plus staff line positions) to compute correct pitches.

Reference: see choirreader/correction/tests/test_pitch.py for the test cases
that demonstrated this bug.
"""

import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path


def _get_staff_lines(root: ET.Element) -> dict[int, list[float]]:
    """Extract the 5 staff line y-coordinates for each staff.

    Returns: {staff_id: [bottom_y, ..., top_y]} sorted descending (bottom first).
    """
    staff_lines = {}
    for staff in root.iter("staff"):
        sid = staff.get("id")
        if sid is None:
            continue
        sid = int(sid)
        lines_elem = staff.find("lines")
        if lines_elem is None:
            continue
        ys = []
        for line in lines_elem.findall("line"):
            points = line.findall("point")
            if points:
                ys.append(float(points[0].get("y")))
        if len(ys) >= 5:
            # Sort descending: highest y (bottom line in image coords) first
            staff_lines[sid] = sorted(ys, reverse=True)[:5]
    return staff_lines


def _get_key_signature(root: ET.Element) -> int:
    """Extract key signature fifths from the .omr file (default 0 = C major)."""
    for k in root.iter("key"):
        fifths = k.get("fifths")
        if fifths is not None:
            return int(fifths)
    return 0


def _get_clef_per_staff(root: ET.Element) -> dict[int, str]:
    """Detect clef for each staff: 'G' (treble) or 'F' (bass).

    Audiveris uses the `shape` attribute on clef elements.
    """
    clefs = {}
    for c in root.iter("clef"):
        shape = c.get("shape", "")
        staff = c.get("staff")
        if staff and shape:
            clefs[int(staff)] = shape
    return clefs


def _y_to_diatonic_step(y: float, staff_lines: list[float]) -> int:
    """Convert a y-coordinate to a diatonic step from the bottom line.

    Returns 0 for the bottom line, +1 for the space above, +2 for the second line, etc.
    Negative values are below the staff.
    """
    bottom_y = staff_lines[0]
    top_y = staff_lines[4]
    line_spacing = (bottom_y - top_y) / 4
    steps = (bottom_y - y) / line_spacing
    return round(steps)


# Diatonic note names for each clef, starting from the bottom line
# In F major (fifths=-1), B is flat; otherwise natural
# The "octave_break" index is where the next octave begins (at note C)
TREBLE_BASE = ["E", "F", "G", "A", "B", "C", "D", "E", "F"]  # bottom to top
BASS_BASE = ["G", "A", "B", "C", "D", "E", "F", "G", "A"]
TREBLE_BELOW = ["D", "C", "B", "A", "G", "F"]  # below bottom line
BASS_BELOW = ["F", "E", "D", "C", "B", "A"]


def _diatonic_to_pitch(step: int, clef: str, fifths: int) -> str:
    """Convert a diatonic step to a note name (with accidental from key signature).

    step=0 is the bottom line of the staff. Positive = above, negative = below.
    The octave boundary is at C (step 5 for treble, step 3 for bass).
    """
    if clef == "G_CLEF":
        scale = TREBLE_BASE
        below = TREBLE_BELOW
        octave = 4
        octave_break = 5  # C is at index 5 in TREBLE_BASE
    else:  # F_CLEF
        scale = BASS_BASE
        below = BASS_BELOW
        octave = 2
        octave_break = 3  # C is at index 3 in BASS_BASE

    if step >= 0 and step < len(scale):
        note = scale[step]
        # Apply key signature (B is flat if fifths=-1)
        if note == "B" and fifths <= -1:
            note = "Bb"
        # Octave increments at C (step >= octave_break)
        return f"{note}{octave + (1 if step >= octave_break else 0)}"
    elif step < 0:
        idx = abs(step) - 1
        if idx < len(below):
            note = below[idx]
            if note == "B" and fifths <= -1:
                note = "Bb"
            # Below the staff: first space below is still same octave
            # (e.g., space below E4 line = D4, not D3)
            # Going further below crosses octave at C
            # step -1 = D (same octave), step -2 = C (same octave),
            # step -3 = B (one octave below)
            below_octave_break = 2  # C is at index 1 in TREBLE_BELOW, B at 2
            return f"{note}{octave - (1 if idx >= below_octave_break else 0)}"
        return f"below_{step}"
    else:
        # Above the staff
        idx = step - len(scale) + 1
        notes_above = ["G", "A", "B", "C", "D", "E"]
        if idx - 1 < len(notes_above):
            note = notes_above[idx - 1]
            if note == "B" and fifths <= -1:
                note = "Bb"
            return f"{note}{octave + 1}"
        return f"above_{step}"


def recalculate_pitches(omr_path: Path) -> dict[int, dict]:
    """Recalculate correct pitches for all noteheads in an .omr file.

    Returns: {head_id: {"staff": int, "x": float, "cy": float, "correct_pitch": int}}

    The correct_pitch is a diatonic step from the bottom line (0=bottom line, +1=above, etc.)
    — the same format as Audiveris' pitch field, but computed from y-coordinates.
    """
    with zipfile.ZipFile(omr_path) as z:
        omr_xml = z.read("sheet#1/sheet#1.xml")
    root = ET.fromstring(omr_xml)

    staff_lines = _get_staff_lines(root)
    if not staff_lines:
        return {}

    result = {}
    for head in root.iter("head"):
        hid = int(head.get("id"))
        staff = int(head.get("staff"))
        bounds = head.find("bounds")
        if bounds is None or staff not in staff_lines:
            continue
        hx = float(bounds.get("x"))
        hy = float(bounds.get("y"))
        hw = float(bounds.get("w"))
        hh = float(bounds.get("h"))
        cy = hy + hh / 2
        correct_step = _y_to_diatonic_step(cy, staff_lines[staff])
        result[hid] = {
            "staff": staff,
            "x": hx,
            "cy": cy,
            "omr_pitch": int(head.get("pitch", 0)),
            "correct_pitch": correct_step,
        }
    return result


def get_key_info(omr_path: Path) -> tuple[int, dict[int, str]]:
    """Extract key signature (fifths) and clef per staff from .omr file."""
    with zipfile.ZipFile(omr_path) as z:
        omr_xml = z.read("sheet#1/sheet#1.xml")
    root = ET.fromstring(omr_xml)
    return _get_key_signature(root), _get_clef_per_staff(root)


def pitch_to_note_name(step: int, staff: int, fifths: int, clef_per_staff: dict) -> str:
    """Convert a diatonic step + staff to a human-readable note name."""
    clef = clef_per_staff.get(staff, "G_CLEF" if staff % 2 == 1 else "F_CLEF")
    return _diatonic_to_pitch(step, clef, fifths)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: pitch_recalc.py <omr_file>")
        sys.exit(1)
    omr_file = Path(sys.argv[1])
    result = recalculate_pitches(omr_file)
    fifths, clefs = get_key_info(omr_file)
    print(f"Key signature: {fifths} fifths")
    print(f"Found {len(result)} noteheads")
    # Show first 10
    for hid, info in list(result.items())[:10]:
        note = pitch_to_note_name(info["correct_pitch"], info["staff"], fifths, clefs)
        print(f"  Head {hid}: staff={info['staff']} y={info['cy']:.0f} "
              f"omr_pitch={info['omr_pitch']:+d} → correct={info['correct_pitch']:+d} ({note})")