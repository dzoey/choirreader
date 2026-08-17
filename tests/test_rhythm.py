"""Tests for rhythm normalization (measure duration quantization)."""

from pathlib import Path
from xml.etree import ElementTree as ET

from choirreader.correction import rhythm


def _make_mxl(part_xml: str) -> Path:
    xml = f"""<?xml version="1.0"?>
<score-partwise version="4.0">
  <part-list>
    <score-part id="P1"><part-name>Voice</part-name></score-part>
  </part-list>
  <part id="P1">
    {part_xml}
  </part>
</score-partwise>"""
    p = Path("/tmp/test_rhythm.musicxml")
    p.write_text(xml)
    return p


def test_underfull_measure_gets_rest():
    # 3/4 measure (divisions=4 -> 12) with only a half note (8) -> add a rest.
    part = """
    <measure number="1">
      <attributes>
        <divisions>4</divisions>
        <time><beats>3</beats><beat-type>4</beat-type></time>
      </attributes>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>8</duration><voice>1</voice><type>half</type></note>
    </measure>
    """
    path = _make_mxl(part)
    summary = rhythm.normalize(path)
    assert summary["rests_added"] == 1

    tree = ET.parse(path)
    notes = tree.getroot().findall(".//note")
    assert len(notes) == 2
    assert notes[1].find("rest") is not None
    assert notes[1].find("duration").text == "4"


def test_overfull_measure_trims_straddling_note():
    # 3/4 measure with a half note + a quarter note (8+4=12) is fine, but a
    # half + half (8+8=16) is overfull -> the second half should be truncated.
    part = """
    <measure number="1">
      <attributes>
        <divisions>4</divisions>
        <time><beats>3</beats><beat-type>4</beat-type></time>
      </attributes>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>8</duration><voice>1</voice><type>half</type></note>
      <note><pitch><step>D</step><octave>4</octave></pitch><duration>8</duration><voice>1</voice><type>half</type></note>
    </measure>
    """
    path = _make_mxl(part)
    summary = rhythm.normalize(path)
    assert summary["notes_trimmed"] == 1

    tree = ET.parse(path)
    notes = tree.getroot().findall(".//note")
    # Second note truncated to a quarter (duration 4).
    assert notes[1].find("duration").text == "4"
    assert notes[1].find("type").text == "quarter"


def test_chord_note_duration_matches_anchor():
    # A chord where the anchor is a half note but the chord note says quarter.
    part = """
    <measure number="1">
      <attributes>
        <divisions>4</divisions>
        <time><beats>3</beats><beat-type>4</beat-type></time>
      </attributes>
      <note><pitch><step>B</step><octave>2</octave></pitch><duration>8</duration><voice>1</voice><type>half</type></note>
      <note><chord/><pitch><step>C</step><octave>3</octave></pitch><duration>4</duration><voice>1</voice><type>quarter</type></note>
    </measure>
    """
    path = _make_mxl(part)
    summary = rhythm.normalize(path)
    # The chord note's duration should be fixed to match the anchor (8).
    tree = ET.parse(path)
    notes = tree.getroot().findall(".//note")
    assert notes[1].find("duration").text == "8"
    assert notes[1].find("type").text == "half"


def test_correct_measure_untouched():
    part = """
    <measure number="1">
      <attributes>
        <divisions>4</divisions>
        <time><beats>3</beats><beat-type>4</beat-type></time>
      </attributes>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>4</duration><voice>1</voice><type>quarter</type></note>
      <note><pitch><step>D</step><octave>4</octave></pitch><duration>4</duration><voice>1</voice><type>quarter</type></note>
      <note><pitch><step>E</step><octave>4</octave></pitch><duration>4</duration><voice>1</voice><type>quarter</type></note>
    </measure>
    """
    path = _make_mxl(part)
    summary = rhythm.normalize(path)
    assert summary["measures_fixed"] == 0
