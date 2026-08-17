"""Tests for the MusicXML sanitizer (fixes OMR output before rendering)."""

from pathlib import Path
from xml.etree import ElementTree as ET

from choirreader.correction import sanitize


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
    p = Path("/tmp/test_sanitize.musicxml")
    p.write_text(xml)
    return p


def test_adds_missing_clef_from_later_measure():
    # Part has no clef in measure 1, but an F clef in measure 2.
    part = """
    <measure number="1">
      <attributes><divisions>1</divisions></attributes>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration></note>
    </measure>
    <measure number="2">
      <attributes>
        <divisions>1</divisions>
        <clef><sign>F</sign><line>4</line></clef>
      </attributes>
      <note><pitch><step>C</step><octave>3</octave></pitch><duration>1</duration></note>
    </measure>
    """
    path = _make_mxl(part)
    summary = sanitize.sanitize(path)
    assert summary["clefs_added"] == 1

    tree = ET.parse(path)
    m1 = tree.getroot().find(".//measure[@number='1']")
    clef = m1.find("attributes/clef")
    assert clef is not None
    assert clef.find("sign").text == "F"
    assert clef.find("line").text == "4"


def test_defaults_to_treble_when_no_clef_anywhere():
    part = """
    <measure number="1">
      <attributes><divisions>1</divisions></attributes>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration></note>
    </measure>
    """
    path = _make_mxl(part)
    summary = sanitize.sanitize(path)
    assert summary["clefs_added"] == 1

    tree = ET.parse(path)
    clef = tree.getroot().find(".//measure[@number='1']/attributes/clef")
    assert clef.find("sign").text == "G"
    assert clef.find("line").text == "2"


def test_leaves_existing_clef_alone():
    part = """
    <measure number="1">
      <attributes>
        <divisions>1</divisions>
        <clef><sign>G</sign><line>2</line></clef>
      </attributes>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration></note>
    </measure>
    """
    path = _make_mxl(part)
    summary = sanitize.sanitize(path)
    assert summary["clefs_added"] == 0
