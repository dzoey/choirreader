"""Unit tests for ChoirReader's pure-logic pieces (no external binaries)."""

from pathlib import Path
from xml.etree import ElementTree as ET

from choirreader.correction.apply import (
    _parse_pitch,
    _parse_duration,
    apply_corrections,
)
from choirreader.correction.vision import _parse_json_array


def test_parse_pitch():
    assert _parse_pitch("C4") == ("C", 4, 0)
    assert _parse_pitch("A#4") == ("A", 4, 1)
    assert _parse_pitch("Bb3") == ("B", 3, -1)
    assert _parse_pitch("") == (None, None, None)
    assert _parse_pitch("garbage") == (None, None, None)


def test_parse_duration():
    assert _parse_duration("quarter") == 1
    assert _parse_duration("half") == 2
    assert _parse_duration("eighth") == 0.5
    assert _parse_duration("2") == 2.0
    assert _parse_duration(None) is None
    assert _parse_duration("bogus") is None


def test_parse_json_array_plain():
    assert _parse_json_array('[{"a": 1}]') == [{"a": 1}]


def test_parse_json_array_with_fence():
    text = '```json\n[{"type": "chord"}]\n```'
    assert _parse_json_array(text) == [{"type": "chord"}]


def test_parse_json_array_with_prose():
    text = 'Here are the issues: [{"type": "chord"}] done.'
    assert _parse_json_array(text) == [{"type": "chord"}]


def _make_mxl(notes_xml: str) -> Path:
    """Build a minimal MusicXML file with the given <note> elements."""
    xml = f"""<?xml version="1.0"?>
<score-partwise version="4.0">
  <part-list>
    <score-part id="P1"><part-name>Voice</part-name></score-part>
  </part-list>
  <part id="P1">
    <measure number="1">
      <attributes><divisions>1</divisions></attributes>
      {notes_xml}
    </measure>
  </part>
</score-partwise>"""
    p = Path("/tmp/test_apply.musicxml")
    p.write_text(xml)
    return p


def test_apply_chord_correction():
    # Two notes at the same default-x should become a chord.
    notes = """
      <note default-x="100"><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration></note>
      <note default-x="100"><pitch><step>A</step><octave>4</octave></pitch><duration>1</duration></note>
    """
    path = _make_mxl(notes)
    disc = [{"type": "chord", "measure": 1}]
    summary = apply_corrections(path, disc)
    assert summary["applied"] == 1

    tree = ET.parse(path)
    notes_el = tree.getroot().findall(".//note")
    assert notes_el[1].find("chord") is not None


def test_apply_time_signature():
    notes = """
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration></note>
    """
    path = _make_mxl(notes)
    disc = [{"type": "time_signature", "expected": "3/4"}]
    summary = apply_corrections(path, disc)
    assert summary["applied"] == 1

    tree = ET.parse(path)
    time = tree.getroot().find(".//time")
    assert time.find("beats").text == "3"
    assert time.find("beat-type").text == "4"
