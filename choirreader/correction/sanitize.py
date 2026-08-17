"""Sanitize MusicXML produced by OMR engines before rendering/correction.

Audiveris (and other OMR engines) sometimes emit structurally-valid but
semantically-broken MusicXML. The most common problem we hit: a part whose
first measure has no <clef>, so downstream tools (musicxml2ly) crash when they
try to resolve a note's pitch against a missing clef.

This module fixes those up in place so the renderer and correction loop get
clean input.
"""

from __future__ import annotations

import logging
from pathlib import Path
from xml.etree import ElementTree as ET

log = logging.getLogger("choirreader.correction.sanitize")

# Default clefs by part name/abbreviation, used only when a part has no clef
# anywhere (so we can't infer it). Treble is the safe default for unknown.
_DEFAULT_CLEF = {"sign": "G", "line": "2"}


def sanitize(musicxml_path: Path) -> dict:
    """Fix up a MusicXML file in place. Returns a summary of what changed."""
    tree, root = _load(musicxml_path)
    summary = {"clefs_added": 0}

    for part in root.findall(".//part"):
        measures = part.findall("measure")
        if not measures:
            continue
        first = measures[0]
        if _has_clef(first):
            continue
        # Find the first clef anywhere in this part to infer the right one.
        clef = _first_clef_in_part(part)
        if clef is None:
            # No clef anywhere — fall back to a default (treble).
            clef = _DEFAULT_CLEF
        _inject_clef(first, clef)
        summary["clefs_added"] += 1
        log.info(
            "Added clef %s/%s to part %s measure 1",
            clef.get("sign"), clef.get("line"), part.get("id"),
        )

    _save(tree, musicxml_path)
    return summary


def _has_clef(measure) -> bool:
    attrs = measure.find("attributes")
    if attrs is None:
        return False
    clef = attrs.find("clef")
    if clef is None:
        return False
    return clef.find("sign") is not None and clef.find("line") is not None


def _first_clef_in_part(part) -> dict | None:
    for measure in part.findall("measure"):
        attrs = measure.find("attributes")
        if attrs is None:
            continue
        clef = attrs.find("clef")
        if clef is None:
            continue
        sign = clef.find("sign")
        line = clef.find("line")
        if sign is not None and line is not None:
            return {"sign": sign.text, "line": line.text}
    return None


def _inject_clef(measure, clef: dict) -> None:
    """Add a <clef> to the measure's <attributes> (creating it if needed)."""
    attrs = measure.find("attributes")
    if attrs is None:
        # Insert <attributes> as the first child of the measure.
        attrs = ET.Element("attributes")
        measure.insert(0, attrs)
    clef_el = ET.SubElement(attrs, "clef")
    ET.SubElement(clef_el, "sign").text = clef["sign"]
    ET.SubElement(clef_el, "line").text = clef["line"]


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
