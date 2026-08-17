"""OMR backend interface and implementations.

The OMR backend turns a PDF (or image) into MusicXML. It is a swappable
interface so we can add engines (oemer, a from-scratch engine, etc.) later
without touching the correction loop.
"""

from __future__ import annotations

import abc
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path


log = logging.getLogger("choirreader.omr")


class OMRError(Exception):
    """Raised when an OMR backend fails to produce MusicXML."""


class OMRBackend(abc.ABC):
    """Abstract OMR engine."""

    name: str = "base"

    @abc.abstractmethod
    def transcribe(self, pdf_path: Path, output_dir: Path) -> list[Path]:
        """Transcribe a PDF into one MusicXML file per page.

        Returns a list of paths to the produced ``.mxl`` (or ``.musicxml``)
        files, in page order.
        """

    def is_available(self) -> bool:
        """Return True if the backend's dependencies are installed."""
        return True


class AudiverisBackend(OMRBackend):
    """Audiveris OMR engine (best free OMR; already installed on this box)."""

    name = "audiveris"

    def __init__(self, binary: str = "audiveris"):
        self.binary = binary

    def is_available(self) -> bool:
        return shutil.which(self.binary) is not None

    def transcribe(self, pdf_path: Path, output_dir: Path) -> list[Path]:
        if not self.is_available():
            raise OMRError(
                f"Audiveris binary '{self.binary}' not found on PATH. "
                "Install Audiveris or set CHOIRREADER_AUDIVERIS_BIN."
            )

        output_dir.mkdir(parents=True, exist_ok=True)

        # Audiveris can take a PDF directly, but page-by-page PNG gives us
        # cleaner per-page control and matches how we render for comparison.
        pages = _pdf_to_pngs(pdf_path, output_dir / "_pages")

        results: list[Path] = []
        failures: list[str] = []
        for i, page in enumerate(pages, start=1):
            page_out = output_dir / f"page_{i:03d}"
            page_out.mkdir(parents=True, exist_ok=True)
            cmd = [
                self.binary,
                "-batch",
                "-transcribe",
                "-export",
                "-output",
                str(page_out),
                str(page),
            ]
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=600
            )
            if proc.returncode != 0:
                # A single bad page (blank, title page, too-large image) should
                # not abort the whole run — log and continue.
                failures.append(
                    f"page {i} ({page.name}): {proc.stderr[-300:].strip()}"
                )
                continue
            mxl = _find_mxl(page_out)
            if mxl is None:
                # Audiveris may skip non-music pages; that's not fatal.
                failures.append(f"page {i} ({page.name}): no MusicXML produced")
                continue
            results.append(mxl)

        if not results:
            detail = "\n".join(failures) if failures else "no recognizable music"
            raise OMRError(
                f"No MusicXML produced from {pdf_path.name}.\n{detail}"
            )
        if failures:
            log.warning(
                "Skipped %d page(s) of %s:\n%s",
                len(failures), pdf_path.name, "\n".join(failures),
            )
        return results


def _pdf_to_pngs(pdf_path: Path, out_dir: Path) -> list[Path]:
    """Render each PDF page to a PNG using pdftoppm (poppler-utils).

    Audiveris rejects images over 20,000,000 pixels, so we render at a DPI
    that keeps pages under that ceiling (and downscale defensively).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    if not shutil.which("pdftoppm"):
        raise OMRError("pdftoppm not found — install poppler-utils.")
    subprocess.run(
        ["pdftoppm", "-png", "-r", "150", str(pdf_path), str(out_dir / "page")],
        check=True,
        capture_output=True,
    )
    pages = sorted(out_dir.glob("page-*.png"))
    # Downscale any page that still exceeds Audiveris' 20M-pixel ceiling.
    from PIL import Image

    for page in pages:
        with Image.open(page) as im:
            w, h = im.size
            if w * h > 20_000_000:
                scale = (20_000_000 / (w * h)) ** 0.5
                new_size = (int(w * scale), int(h * scale))
                im.thumbnail(new_size, Image.LANCZOS)
                im.save(page)
    return pages


def _find_mxl(directory: Path) -> Path | None:
    """Return the first .mxl or .musicxml file in a directory, if any."""
    for pattern in ("*.mxl", "*.musicxml", "*.xml"):
        found = sorted(directory.glob(pattern))
        if found:
            return found[0]
    return None


def get_backend(name: str, config) -> OMRBackend:
    """Factory: return the OMR backend for a given name."""
    if name == "audiveris":
        return AudiverisBackend(binary=config.audiveris_bin)
    raise OMRError(f"Unknown OMR backend: {name!r}")
