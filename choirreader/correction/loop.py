"""The correction loop: OCR → compare → fix → repeat.

Orchestrates the full pipeline for a single page (or a whole PDF):

1. OMR backend produces MusicXML.
2. Render MusicXML to an image.
3. Vision model compares it to the original page image.
4. Apply corrections to the MusicXML.
5. Repeat 2–4 up to ``max_iterations`` (default 3).
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import Config
from ..omr import get_backend
from . import apply, render, sanitize, vision

log = logging.getLogger("choirreader.correction.loop")


class CorrectionResult:
    """Outcome of transcribing one page."""

    def __init__(self, page_number: int, musicxml: Path | None):
        self.page_number = page_number
        self.musicxml = musicxml
        self.iterations: list[dict] = []  # per-iteration summaries

    @property
    def total_applied(self) -> int:
        return sum(it.get("applied", 0) for it in self.iterations)


def transcribe_pdf(pdf_path: Path, output_dir: Path, config: Config) -> list[CorrectionResult]:
    """Transcribe a whole PDF, running the correction loop per page."""
    backend = get_backend(config.omr_backend, config)
    mxl_files = backend.transcribe(pdf_path, output_dir)

    results: list[CorrectionResult] = []
    for i, mxl in enumerate(mxl_files, start=1):
        res = CorrectionResult(i, mxl)
        # Sanitize OMR output (e.g. add missing clefs) before rendering, so
        # downstream tools like musicxml2ly don't crash on malformed input.
        try:
            sanitize.sanitize(mxl)
        except Exception as e:  # noqa: BLE001 — sanitize is best-effort
            log.warning("Sanitize failed on %s: %s", mxl.name, e)
        if config.vision_model and config.max_iterations > 0:
            _run_correction_loop(mxl, pdf_path, i, config, res)
        results.append(res)
    return results


def _run_correction_loop(
    mxl: Path, pdf_path: Path, page_number: int, config: Config, result: CorrectionResult
) -> None:
    """Run the compare/fix loop for one page's MusicXML."""
    # Original page image (re-render from PDF for a clean, aligned reference).
    page_png = _page_png(pdf_path, page_number, mxl.parent)

    for iteration in range(1, config.max_iterations + 1):
        log.info("Page %d iteration %d/%d", page_number, iteration, config.max_iterations)

        # Render current MusicXML to an image.
        rendered = mxl.parent / f"page_{page_number:03d}_iter{iteration}.png"
        try:
            render.render_musicxml_to_png(mxl, rendered, config.renderer)
        except render.RenderError as e:
            log.warning("Render failed, stopping loop: %s", e)
            break

        # Ask the vision model to diff.
        try:
            discrepancies = vision.compare_pages(
                page_png, rendered, config.vision_model, config.ollama_url
            )
        except vision.VisionError as e:
            log.warning("Vision comparison failed, stopping loop: %s", e)
            break

        if not discrepancies:
            log.info("No discrepancies found on iteration %d — done.", iteration)
            result.iterations.append({"iteration": iteration, "applied": 0, "discrepancies": 0})
            break

        # Apply them.
        summary = apply.apply_corrections(mxl, discrepancies)
        summary["iteration"] = iteration
        summary["discrepancies"] = len(discrepancies)
        result.iterations.append(summary)
        log.info(
            "Iteration %d: %d discrepancies, %d applied, %d skipped",
            iteration, len(discrepancies), summary["applied"], summary["skipped"],
        )


def _page_png(pdf_path: Path, page_number: int, out_dir: Path) -> Path:
    """Render a single PDF page to PNG (for the vision model's reference)."""
    import shutil
    import subprocess

    out = out_dir / f"page_{page_number:03d}_original.png"
    if out.exists():
        return out
    if not shutil.which("pdftoppm"):
        raise RuntimeError("pdftoppm not found — install poppler-utils.")
    subprocess.run(
        ["pdftoppm", "-png", "-r", "200", "-f", str(page_number),
         "-l", str(page_number), str(pdf_path), str(out_dir / f"page_{page_number:03d}_orig")],
        check=True, capture_output=True,
    )
    # pdftoppm names it page_NNN_orig-<page>.png
    produced = sorted(out_dir.glob(f"page_{page_number:03d}_orig-*.png"))
    if produced:
        produced[0].rename(out)
    return out
