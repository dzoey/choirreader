"""OMR backend that reads Audiveris .omr files directly and generates MusicXML.

Bypasses Audiveris' MusicXML export which has two major bugs:
1. Flattens chords — ~98% of detected chord groupings are lost
2. Inverts pitches for multi-notehead chords (87-97% across our test pages)

The internal .omr file has the correct notehead positions, chord groupings,
voice assignments, and rhythm. This module reads that data and constructs
MusicXML from scratch with correct pitches, chords, and rhythm.
"""

from __future__ import annotations

import logging
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

from ..correction import pitch
from ..correction.pitch import _get_staff_lines

log = logging.getLogger("choirreader.omr.omr_direct")

DIVISIONS = 4


@dataclass
class Notehead:
    """A single notehead in the .omr file."""
    id: int
    staff: int
    omr_pitch: int
    correct_step: float
    x: float
    y: float
    cy: float


@dataclass
class Chord:
    """A chord (one or more noteheads on a stem)."""
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
    """Convert step to (step_letter, octave, alter).

    alter: -1 for flat, 0 for natural, +1 for sharp.
    """
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


def parse_omr(omr_path: Path) -> Score:
    with zipfile.ZipFile(omr_path) as z:
        omr_xml = z.read("sheet#1/sheet#1.xml")
    root = ET.fromstring(omr_xml)

    score = Score()
    staff_lines = pitch._get_staff_lines(root)

    for k in root.iter("key"):
        fifths = k.get("fifths")
        if fifths is not None:
            score.key_fifths = int(fifths)
            break

    for m in root.iter("measure"):
        times_ref = m.find("times")
        if times_ref is not None and times_ref.text:
            time_id = int(times_ref.text)
            for tp in root.iter("time-pair"):
                if int(tp.get("id")) == time_id:
                    rational = tp.get("time-rational", "3/4")
                    num, den = rational.split("/")
                    score.time_sig = (int(num), int(den))
                    break
            break

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

        measure_data = {"id": mid, "voices": {}}
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
                            slots.append({"key": key_val, "chord_id": chord_id, "status": status})
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
    if duration_divisions >= 16:
        return "whole"
    elif duration_divisions >= 8:
        return "half"
    elif duration_divisions >= 4:
        return "quarter"
    elif duration_divisions >= 2:
        return "eighth"
    elif duration_divisions >= 1:
        return "16th"
    return "quarter"


def _build_part_from_staff(score: Score, staff_id: int, part_id: str) -> ET.Element:
    sdata = score.staves[staff_id]
    part = ET.Element("part")
    part.set("id", part_id)

    clef_sign = "G" if sdata.clef == "G_CLEF" else "F"
    clef_line = "2" if sdata.clef == "G_CLEF" else "4"

    all_measures = sdata.measures
    time_num = score.time_sig[0]

    for measure_idx, measure in enumerate(all_measures, start=1):
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
            beats.text = str(score.time_sig[0])
            bt = ET.SubElement(time, "beat-type")
            bt.text = str(score.time_sig[1])

        for voice_id in sorted(measure["voices"].keys()):
            voice_slots = measure["voices"][voice_id]
            if voice_slots:
                _emit_slots(m_xml, voice_slots, sdata, score, staff_id, voice_id, time_num)

    return part


def _emit_slots(measure_xml: ET.Element, slots: list[dict], sdata: StaffData,
                 score: Score, staff_id: int, voice_id: int, time_num: int) -> None:
    sorted_slots = sorted(slots, key=lambda s: s["key"] if s["key"] is not None else 0)
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

        current_key = slot["key"]
        if idx_pos + 1 < len(begin_indices):
            next_begin = sorted_slots[begin_indices[idx_pos + 1]]
            next_key = next_begin["key"]
        else:
            next_key = time_num + 1

        if next_key is None or next_key <= current_key:
            duration_div = DIVISIONS
        else:
            duration_div = (next_key - current_key) * DIVISIONS

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
    root = ET.Element("score-partwise")
    root.set("version", "4.0")

    part_list = ET.SubElement(root, "part-list")
    score_part = ET.SubElement(part_list, "score-part")
    score_part.set("id", "P1")
    part_name = ET.SubElement(score_part, "part-name")
    part_name.text = "Music"

    for i, staff_id in enumerate(sorted(score.staves.keys()), start=1):
        part_xml = _build_part_from_staff(score, staff_id, f"P{i}")
        root.append(part_xml)

    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


def convert_omr_to_musicxml(omr_path: Path, output_path: Path) -> dict:
    """Convert an .omr file to MusicXML.

    If the output_path ends in .mxl, the result is compressed into a ZIP
    with META-INF/container.xml. Otherwise, raw XML is written.
    """
    score = parse_omr(omr_path)
    xml_str = generate_musicxml(score)

    if str(output_path).endswith(".mxl"):
        # Compress into .mxl (ZIP) format
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
