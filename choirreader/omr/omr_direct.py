"""OMR backend that reads Audiveris .omr files directly and generates MusicXML.

Bypasses Audiveris' MusicXML export which has two major bugs:
1. Flattens chords — ~98% of detected chord groupings are lost
2. Inverts pitches for multi-notehead chords (87-97% across our test pages)

The internal .omr file has correct data:
- Notehead y-coordinates (visual positions)
- Chord groupings (multi-notehead chords)
- Voice assignments (soprano/alto/tenor/bass)
- Rhythm via slot time-offsets (fractional whole notes)

This module reads that data and constructs MusicXML with correct pitches,
chords, and rhythm.

Key insight: Audiveris uses slot time-offsets like "1/8", "3/4", "1/2" etc.
to represent time positions within a measure. These are fractions of a whole
note. Duration = (next_BEGIN_slot_time - current_slot_time) * 4 * divisions.
"""

from __future__ import annotations

import logging
import zipfile
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from xml.etree import ElementTree as ET

from ..correction import pitch
from ..correction.pitch import _get_staff_lines

log = logging.getLogger("choirreader.omr.omr_direct")

DIVISIONS = 4  # divisions per quarter note


@dataclass
class Notehead:
    id: int
    staff: int
    omr_pitch: int
    correct_step: float
    x: float
    y: float
    cy: float


@dataclass
class Chord:
    id: int
    staff: int
    x: float
    y: float
    w: float
    h: float
    noteheads: list[Notehead] = field(default_factory=list)


@dataclass
class StaffData:
    staff_id: int
    clef: str
    measures: list[dict] = field(default_factory=list)
    chords: dict[int, Chord] = field(default_factory=dict)


@dataclass
class Score:
    time_sig: tuple[int, int] = (3, 4)
    key_fifths: int = 0
    staves: dict[int, StaffData] = field(default_factory=dict)
    rests: list[dict] = field(default_factory=list)


def _y_to_diatonic_step_fine(y: float, staff_lines: list[float]) -> float:
    bottom_y = staff_lines[0]
    top_y = staff_lines[4]
    line_spacing = (bottom_y - top_y) / 4
    steps = (bottom_y - y) / line_spacing
    return round(steps * 2) / 2


TREBLE_NOTE_TABLE = [
    (-3.0, "F", 3), (-2.5, "G", 3), (-2.0, "A", 3), (-1.5, "B", 3),
    (-1.0, "C", 4), (-0.5, "D", 4),
    (0.0, "E", 4), (0.5, "F", 4), (1.0, "G", 4), (1.5, "A", 4),
    (2.0, "B", 4), (2.5, "C", 5), (3.0, "D", 5), (3.5, "E", 5),
    (4.0, "F", 5), (4.5, "G", 5), (5.0, "A", 5), (5.5, "B", 5),
    (6.0, "C", 6),
]

BASS_NOTE_TABLE = [
    (-3.0, "A", 1), (-2.5, "B", 1), (-2.0, "C", 2), (-1.5, "D", 2),
    (-1.0, "E", 2), (-0.5, "F", 2),
    (0.0, "G", 2), (0.5, "A", 2), (1.0, "B", 2), (1.5, "C", 3),
    (2.0, "D", 3), (2.5, "E", 3), (3.0, "F", 3), (3.5, "G", 3),
    (4.0, "A", 3), (4.5, "B", 3), (5.0, "C", 4),
]


def _fine_step_to_diatonic(step: float, clef: str, fifths: int) -> tuple[str, int, int]:
    table = TREBLE_NOTE_TABLE if clef == "G_CLEF" else BASS_NOTE_TABLE
    best = None
    best_diff = float('inf')
    for entry_step, note, octave in table:
        diff = abs(entry_step - step)
        if diff < best_diff:
            best_diff = diff
            best = (note, octave)

    if best is None:
        return "C", 4, 0

    note, octave = best
    alter = 0
    if note == "B" and fifths <= -1:
        alter = -1
    return note, octave, alter


def _parse_time_offset(s: str) -> Fraction:
    """Parse a time-offset string like '1/4', '3/8', '1/16' into a Fraction."""
    s = s.strip()
    if '/' in s:
        num, den = s.split('/')
        return Fraction(int(num), int(den))
    return Fraction(int(s))


def _build_slot_time_map(root: ET.Element) -> dict[int, dict[int, Fraction]]:
    """Build a mapping of stack_id -> {slot_id -> time-offset fraction}.

    Each stack represents a measure. Its slots have time-offsets that are
    fractions of a whole note (e.g., 1/4 = quarter note from start of measure).
    """
    stack_map = {}
    for stack in root.iter('stack'):
        sid = int(stack.get('id'))
        slots = {}
        for slot in stack.findall('slot'):
            slot_id = int(slot.get('id'))
            time_str = slot.get('time-offset', '0')
            slots[slot_id] = _parse_time_offset(time_str)
        stack_map[sid] = slots
    return stack_map


def _infer_time_sig_from_stacks(root: ET.Element) -> tuple[int, int]:
    """Infer the time signature from the most common stack duration."""
    durations = []
    for stack in root.iter('stack'):
        d = stack.get('duration', '1')
        durations.append(_parse_time_offset(d))
    if not durations:
        return 4, 4
    # Most common duration
    from collections import Counter
    most_common = Counter(durations).most_common(1)[0][0]
    # Convert Fraction to time signature
    # 3/4 = 3/4, 2/4 = 1/2 (whole notes), 4/4 = 1, 6/8 = 3/4 (whole notes)
    if most_common == Fraction(1):
        return 4, 4
    elif most_common == Fraction(3, 4):
        return 3, 4
    elif most_common == Fraction(1, 2):
        return 2, 4
    elif most_common == Fraction(3, 8):
        return 6, 8
    elif most_common == Fraction(1, 4):
        return 1, 4
    else:
        # Try to find a simple fraction
        num = most_common.numerator
        den = most_common.denominator
        return num, den


def parse_omr(omr_path: Path) -> Score:
    with zipfile.ZipFile(omr_path) as z:
        omr_xml = z.read("sheet#1/sheet#1.xml")
    root = ET.fromstring(omr_xml)

    score = Score()
    staff_lines = pitch._get_staff_lines(root)

    # Key signature
    for k in root.iter("key"):
        fifths = k.get("fifths")
        if fifths is not None:
            score.key_fifths = int(fifths)
            break

    # Time signature - prefer explicit time-pair, else infer from stacks
    found_time = False
    for m in root.iter("measure"):
        times_ref = m.find("times")
        if times_ref is not None and times_ref.text:
            time_id = int(times_ref.text)
            for tp in root.iter("time-pair"):
                if int(tp.get("id")) == time_id:
                    rational = tp.get("time-rational", "3/4")
                    num, den = rational.split("/")
                    score.time_sig = (int(num), int(den))
                    found_time = True
                    break
            break

    if not found_time:
        score.time_sig = _infer_time_sig_from_stacks(root)

    # Clefs
    for c in root.iter("clef"):
        shape = c.get("shape", "")
        staff = c.get("staff")
        if staff and shape:
            sid = int(staff)
            if sid not in score.staves:
                score.staves[sid] = StaffData(staff_id=sid, clef=shape)

    for sid in staff_lines:
        if sid not in score.staves:
            default_clef = "G_CLEF" if sid % 2 == 1 else "F_CLEF"
            score.staves[sid] = StaffData(staff_id=sid, clef=default_clef)

    # Noteheads
    noteheads_by_staff: dict[int, list[Notehead]] = {}
    for head in root.iter("head"):
        hid = int(head.get("id"))
        staff = int(head.get("staff"))
        omr_pitch = int(head.get("pitch", 0))
        bounds = head.find("bounds")
        if bounds is None or staff not in staff_lines:
            continue
        hx = float(bounds.get("x"))
        hy = float(bounds.get("y"))
        hw = float(bounds.get("w"))
        hh = float(bounds.get("h"))
        cy = hy + hh / 2
        correct_step = _y_to_diatonic_step_fine(cy, staff_lines[staff])
        nh = Notehead(
            id=hid, staff=staff, omr_pitch=omr_pitch,
            correct_step=correct_step, x=hx, y=hy, cy=cy,
        )
        noteheads_by_staff.setdefault(staff, []).append(nh)

    # Chords
    for hc in root.iter("head-chord"):
        cid = int(hc.get("id"))
        staff = int(hc.get("staff"))
        bounds = hc.find("bounds")
        if bounds is None or staff not in score.staves:
            continue
        hx = float(bounds.get("x"))
        hy = float(bounds.get("y"))
        hw = float(bounds.get("w"))
        hh = float(bounds.get("h"))
        chord = Chord(id=cid, staff=staff, x=hx, y=hy, w=hw, h=hh)
        for nh in noteheads_by_staff.get(staff, []):
            if nh.x < hx - 5 or nh.x > hx + hw + 5:
                continue
            if nh.cy < hy - 5 or nh.cy > hy + hh + 5:
                continue
            chord.noteheads.append(nh)
        score.staves[staff].chords[cid] = chord

    # Build slot time map
    slot_time_map = _build_slot_time_map(root)

    # Measures
    for m in root.iter("measure"):
        mid = int(m.get("id"))
        hc_ref = m.find("head-chords")
        measure_staff = None
        if hc_ref is not None and hc_ref.text:
            first_chord_id = int(hc_ref.text.split()[0])
            for sid, sdata in score.staves.items():
                if first_chord_id in sdata.chords:
                    measure_staff = sid
                    break

        if measure_staff is None:
            continue

        # Get this measure's stack index (measure IDs restart per staff)
        # Actually the stack ID is the same as the measure ID for the first staff
        # For subsequent staves, the stack ID might be different
        # For simplicity, use the measure's own stack (stacks are per-staff)
        measure_data = {"id": mid, "voices": {}}

        # Find the right stack for this measure
        # Stacks are in order, so we need to figure out which stack corresponds to this measure
        # The slot keys in this measure should match a stack's slot IDs
        # For simplicity, we'll find the stack whose slot IDs match
        stack_id = None
        for voice in m.findall("voice"):
            slots_elem = voice.find("slots")
            if slots_elem is None:
                continue
            for entry in slots_elem.findall("entry"):
                key_elem = entry.find("key")
                if key_elem is not None:
                    key_val = int(key_elem.text)
                    # Find which stack has this slot id
                    for sid_test, slots in slot_time_map.items():
                        if key_val in slots:
                            stack_id = sid_test
                            break
                    break
            if stack_id is not None:
                break

        # Use the matched stack's time map, or default to 1/key
        if stack_id is not None:
            time_map = slot_time_map[stack_id]
        else:
            time_map = None

        for voice in m.findall("voice"):
            vid = int(voice.get("id"))
            slots = []
            slots_elem = voice.find("slots")
            if slots_elem is not None:
                for entry in slots_elem.findall("entry"):
                    key_elem = entry.find("key")
                    key_val = int(key_elem.text) if key_elem is not None else None
                    value_elem = entry.find("value")
                    if value_elem is not None:
                        chord_id = int(value_elem.get("chord"))
                        status = value_elem.get("status", "BEGIN")
                        if chord_id in score.staves[measure_staff].chords:
                            # Convert slot key to time-offset
                            if time_map and key_val in time_map:
                                time_offset = time_map[key_val]
                            else:
                                # Fallback: assume each key = 1 quarter note
                                time_offset = Fraction(key_val, 4) if key_val else Fraction(0)
                            slots.append({
                                "key": key_val,
                                "chord_id": chord_id,
                                "status": status,
                                "time_offset": time_offset,
                            })
            measure_data["voices"][vid] = slots

        score.staves[measure_staff].measures.append(measure_data)

    for r in root.iter("rest"):
        rstaff = int(r.get("staff", 1))
        shape = r.get("shape", "")
        bounds = r.find("bounds")
        rx = float(bounds.get("x")) if bounds is not None else 0
        ry = float(bounds.get("y")) if bounds is not None else 0
        score.rests.append({"staff": rstaff, "shape": shape, "x": rx, "y": ry})

    return score


def _slot_to_type(duration_divisions: int) -> str:
    """Convert duration in divisions to a MusicXML note type."""
    if duration_divisions >= 16:
        return "whole"
    elif duration_divisions >= 12:
        return "dotted-half"
    elif duration_divisions >= 8:
        return "half"
    elif duration_divisions >= 6:
        return "dotted-quarter"
    elif duration_divisions >= 4:
        return "quarter"
    elif duration_divisions >= 3:
        return "dotted-eighth"
    elif duration_divisions >= 2:
        return "eighth"
    elif duration_divisions >= 1:
        return "16th"
    return "quarter"


def _group_staff_pairs(staff_ids: list[int]) -> list[tuple[int, int]]:
    sorted_staves = sorted(staff_ids)
    pairs = []
    for i in range(0, len(sorted_staves), 2):
        if i + 1 < len(sorted_staves):
            pairs.append((sorted_staves[i], sorted_staves[i + 1]))
        else:
            pairs.append((sorted_staves[i], None))
    return pairs


def _build_part_from_staves(
    score: Score,
    treble_staff_ids: list[int],
    bass_staff_ids: list[int],
    part_id: str,
    part_name: str,
    clef: str,
    time_num: int,
    time_den: int,
) -> ET.Element:
    part = ET.Element("part")
    part.set("id", part_id)

    clef_sign = "G" if clef == "G_CLEF" else "F"
    clef_line = "2" if clef == "G_CLEF" else "4"

    all_measures = []
    for staff_id in treble_staff_ids + bass_staff_ids:
        sdata = score.staves[staff_id]
        for m in sdata.measures:
            all_measures.append((staff_id, m))

    # Total duration of the time signature in whole notes
    total_whole_notes = Fraction(time_num, time_den) * Fraction(1, 4)

    for measure_idx, (staff_id, measure) in enumerate(all_measures, start=1):
        sdata = score.staves[staff_id]
        m_xml = ET.SubElement(part, "measure")
        m_xml.set("number", str(measure_idx))

        if measure_idx == 1:
            attrs = ET.SubElement(m_xml, "attributes")
            divs = ET.SubElement(attrs, "divisions")
            divs.text = str(DIVISIONS)
            k = ET.SubElement(attrs, "key")
            fifths = ET.SubElement(k, "fifths")
            fifths.text = str(score.key_fifths)
            c = ET.SubElement(attrs, "clef")
            sign = ET.SubElement(c, "sign")
            sign.text = clef_sign
            line = ET.SubElement(c, "line")
            line.text = clef_line
            time = ET.SubElement(attrs, "time")
            beats = ET.SubElement(time, "beats")
            beats.text = str(time_num)
            bt = ET.SubElement(time, "beat-type")
            bt.text = str(time_den)

        for voice_id in sorted(measure["voices"].keys()):
            voice_slots = measure["voices"][voice_id]
            if voice_slots:
                _emit_slots(m_xml, voice_slots, sdata, score, staff_id, voice_id, total_whole_notes)

    return part


def _emit_slots(measure_xml: ET.Element, slots: list[dict], sdata: StaffData,
                 score: Score, staff_id: int, voice_id: int, total_whole_notes: Fraction) -> None:
    """Emit MusicXML notes from a list of slots using time-offset fractions.

    Duration = (next_BEGIN_slot_time - current_slot_time) * 4 * DIVISIONS
    where 4 converts whole notes to quarter notes.
    """
    sorted_slots = sorted(slots, key=lambda s: s["time_offset"])
    begin_indices = [
        i for i, s in enumerate(sorted_slots)
        if s["status"] == "BEGIN"
    ]

    for idx_pos, i in enumerate(begin_indices):
        slot = sorted_slots[i]
        chord_id = slot["chord_id"]
        chord = sdata.chords.get(chord_id)
        if chord is None or not chord.noteheads:
            continue

        current_time = slot["time_offset"]
        if idx_pos + 1 < len(begin_indices):
            next_begin = sorted_slots[begin_indices[idx_pos + 1]]
            next_time = next_begin["time_offset"]
        else:
            next_time = total_whole_notes

        # Duration in divisions = (next - current) * 4 * DIVISIONS
        # (4 converts whole notes to quarter notes, DIVISIONS converts to divisions)
        duration_whole = next_time - current_time
        duration_div = int(duration_whole * 4 * DIVISIONS)

        if duration_div <= 0:
            duration_div = DIVISIONS  # default to quarter

        noteheads = sorted(chord.noteheads, key=lambda n: n.cy)

        _emit_note(measure_xml, noteheads[0], score, staff_id, voice_id,
                    duration_div, is_chord=False)

        for nh in noteheads[1:]:
            _emit_note(measure_xml, nh, score, staff_id, voice_id,
                        duration_div, is_chord=True)


def _emit_note(measure_xml: ET.Element, notehead: Notehead, score: Score,
                staff_id: int, voice_id: int, duration_div: int, is_chord: bool) -> None:
    note = ET.SubElement(measure_xml, "note")
    if is_chord:
        ET.SubElement(note, "chord")

    sdata = score.staves[staff_id]
    pitch_elem = ET.SubElement(note, "pitch")
    step, octave, alter = _fine_step_to_diatonic(
        notehead.correct_step, sdata.clef, score.key_fifths,
    )
    step_elem = ET.SubElement(pitch_elem, "step")
    step_elem.text = step
    if alter != 0:
        alter_elem = ET.SubElement(pitch_elem, "alter")
        alter_elem.text = str(alter)
    oct_elem = ET.SubElement(pitch_elem, "octave")
    oct_elem.text = str(octave)

    dur = ET.SubElement(note, "duration")
    dur.text = str(duration_div)
    voice_elem = ET.SubElement(note, "voice")
    voice_elem.text = str(voice_id)
    type_elem = ET.SubElement(note, "type")
    type_elem.text = _slot_to_type(duration_div)
    stem = ET.SubElement(note, "stem")
    stem.text = "up"


def generate_musicxml(score: Score) -> str:
    """Generate a MusicXML string with two parts (treble + bass)."""
    root = ET.Element("score-partwise")
    root.set("version", "4.0")

    staff_ids = sorted(score.staves.keys())
    pairs = _group_staff_pairs(staff_ids)

    treble_staves = [p[0] for p in pairs]
    bass_staves = [p[1] for p in pairs if p[1] is not None]

    part_list = ET.SubElement(root, "part-list")
    sp1 = ET.SubElement(part_list, "score-part")
    sp1.set("id", "P1")
    ET.SubElement(sp1, "part-name").text = "Treble"
    sp2 = ET.SubElement(part_list, "score-part")
    sp2.set("id", "P2")
    ET.SubElement(sp2, "part-name").text = "Bass"

    time_num, time_den = score.time_sig

    treble_part = _build_part_from_staves(
        score, treble_staves, [], "P1", "Treble", "G_CLEF",
        time_num, time_den,
    )
    root.append(treble_part)

    if bass_staves:
        bass_part = _build_part_from_staves(
            score, [], bass_staves, "P2", "Bass", "F_CLEF",
            time_num, time_den,
        )
        root.append(bass_part)

    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


def convert_omr_to_musicxml(omr_path: Path, output_path: Path) -> dict:
    score = parse_omr(omr_path)
    xml_str = generate_musicxml(score)

    if str(output_path).endswith(".mxl"):
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("score.xml", xml_str)
            container_xml = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<container>\n'
                '  <rootfiles>\n'
                '    <rootfile full-path="score.xml"/>\n'
                '  </rootfiles>\n'
                '</container>\n'
            )
            z.writestr("META-INF/container.xml", container_xml)
    else:
        output_path.write_text(xml_str, encoding="utf-8")

    total_notes = sum(
        len(c.noteheads)
        for sdata in score.staves.values()
        for c in sdata.chords.values()
    )
    return {
        "notes_emitted": total_notes,
        "chords_emitted": sum(len(s.chords) for s in score.staves.values()),
        "measures": sum(len(s.measures) for s in score.staves.values()),
        "time_sig": f"{score.time_sig[0]}/{score.time_sig[1]}",
        "key_fifths": score.key_fifths,
        "staves": len(score.staves),
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: omr_direct.py <omr_file> <output_mxl>")
        sys.exit(1)
    omr = Path(sys.argv[1])
    out = Path(sys.argv[2])
    stats = convert_omr_to_musicxml(omr, out)
    print(f"Converted {omr} -> {out}")
    print(f"  {stats['staves']} staves, {stats['measures']} measures, "
          f"{stats['chords_emitted']} chords, {stats['notes_emitted']} notes")
    print(f"  Time sig: {stats['time_sig']}, Key fifths: {stats['key_fifths']}")
