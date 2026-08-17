"""Render MusicXML back to an image for side-by-side comparison with the PDF.

We render the (possibly corrected) MusicXML to an image so the vision model
can compare it against the original PDF page. Two renderers are supported:
MuseScore (best fidelity) and Lilypond (lighter, scriptable).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class RenderError(Exception):
    pass


def render_musicxml_to_png(
    musicxml_path: Path, output_png: Path, renderer: str = "musescore"
) -> Path:
    """Render a MusicXML file to a PNG image."""
    if renderer == "musescore":
        return _render_musescore(musicxml_path, output_png)
    if renderer == "lilypond":
        return _render_lilypond(musicxml_path, output_png)
    raise RenderError(f"Unknown renderer: {renderer!r}")


def _render_musescore(musicxml_path: Path, output_png: Path) -> Path:
    bin_ = shutil.which("musescore") or shutil.which("mscore")
    if not bin_:
        raise RenderError("MuseScore not found — install it or use lilypond.")
    output_png.parent.mkdir(parents=True, exist_ok=True)
    # MuseScore needs a display; use offscreen Qt platform.
    env = {"QT_QPA_PLATFORM": "offscreen"}
    cmd = [bin_, "-o", str(output_png), str(musicxml_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=300)
    if proc.returncode != 0 or not output_png.exists():
        raise RenderError(
            f"MuseScore render failed:\n{proc.stderr[-2000:]}"
        )
    return output_png


def _render_lilypond(musicxml_path: Path, output_png: Path) -> Path:
    bin_ = shutil.which("lilypond")
    if not bin_:
        raise RenderError("Lilypond not found.")
    # musicxml2ly converts MusicXML to Lilypond source.
    m2l = shutil.which("musicxml2ly")
    if not m2l:
        raise RenderError("musicxml2ly not found (part of Lilypond).")
    output_png.parent.mkdir(parents=True, exist_ok=True)
    ly = output_png.with_suffix(".ly")
    subprocess.run(
        [m2l, "-o", str(ly), str(musicxml_path)],
        check=True, capture_output=True, timeout=120,
    )
    # Render to PNG. Lilypond names output <stem>-1.png, <stem>-2.png, ...
    subprocess.run(
        [bin_, "--png", "-dno-gs-load-fonts", "-dinclude-eps-fonts",
         "-o", str(output_png.parent / ly.stem), str(ly)],
        check=True, capture_output=True, timeout=300,
    )
    produced = sorted(output_png.parent.glob(f"{ly.stem}-*.png"))
    if not produced:
        produced = sorted(output_png.parent.glob(f"{ly.stem}.png"))
    if not produced:
        raise RenderError("Lilypond render produced no PNG.")
    if len(produced) == 1:
        produced[0].rename(output_png)
    else:
        # Multiple pages: stack them vertically into one image for comparison.
        _stack_pngs(produced, output_png)
    return output_png


def _stack_pngs(pngs: list[Path], output_png: Path) -> None:
    """Stack multiple page PNGs vertically into a single image."""
    from PIL import Image

    images = [Image.open(p) for p in pngs]
    width = max(im.width for im in images)
    height = sum(im.height for im in images)
    canvas = Image.new("RGB", (width, height), "white")
    y = 0
    for im in images:
        canvas.paste(im.convert("RGB"), (0, y))
        y += im.height
    canvas.save(output_png)
    for im in images:
        im.close()
