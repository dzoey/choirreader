# ChoirReader

Optical Music Recognition (OMR) for choral sheet music, with an AI-assisted
correction loop. Upload a PDF of sheet music, get corrected MusicXML (and MIDI)
out.

## What it does

1. **OCR** — runs an OMR engine (Audiveris by default) over each page of the
   PDF to produce MusicXML.
2. **Compare** — renders the MusicXML back to an image and shows it next to the
   original PDF page, then asks a vision model (local Ollama, e.g.
   `minimax-m3:cloud`) to spot discrepancies.
3. **Fix** — applies the corrections back into the MusicXML.
4. **Repeat** — loops steps 2–3 up to a configurable maximum (default 3).

## Why

Free OMR engines (Audiveris, oemer) are good at *detecting* notation but lose
information on export — most notably **chords get flattened into sequential
notes** (a stem with two noteheads becomes two separate notes). ChoirReader
closes that gap with a vision-model correction loop instead of a from-scratch
OMR engine.

## Install

```bash
# Requires: Audiveris (OMR), MuseScore or Lilypond (render), Ollama (vision)
pip install -e .
```

## Usage

### Web UI

```bash
choirreader serve            # http://localhost:8000
```

Upload a PDF, watch the correction loop run, download the final MusicXML/MIDI.

### Headless CLI

```bash
choirreader transcribe input.pdf -o output/ --max-iterations 3
choirreader transcribe input.pdf --no-vision   # skip the correction loop
```

## Configuration

| Setting | Default | Meaning |
|---------|---------|---------|
| `max_iterations` | `3` | Correction-loop passes |
| `omr_backend` | `audiveris` | OMR engine |
| `vision_model` | `minimax-m3:cloud` | Ollama model for comparison |
| `renderer` | `musescore` | MusicXML → image renderer |

## License

MIT
