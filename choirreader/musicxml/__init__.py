"""MusicXML utilities: parse, fix, and export.

Thin wrappers around music21 for the parts of the pipeline that need to
inspect or emit MusicXML/MIDI. Kept separate from the correction loop so the
loop stays engine-agnostic.
"""

from __future__ import annotations

from pathlib import Path


def to_midi(musicxml_path: Path, midi_path: Path) -> Path:
    """Convert a MusicXML file to MIDI using music21."""
    from music21 import converter

    score = converter.parse(str(musicxml_path))
    score.write("midi", str(midi_path))
    return midi_path


def count_notes(musicxml_path: Path) -> dict:
    """Return a quick structural summary of a MusicXML file."""
    from music21 import converter

    score = converter.parse(str(musicxml_path))
    parts = {}
    for p in score.parts:
        notes = p.flatten().notes
        chords = sum(1 for n in notes if n.isChord)
        parts[p.partName or "unnamed"] = {
            "notes": len(notes),
            "chords": chords,
        }
    return parts
