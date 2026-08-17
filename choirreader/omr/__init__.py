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
    """Audiveris OMR engine.

    Runs Audiveris to produce its internal ``.omr`` file (which has correct
    notehead positions, chord groupings, voice assignments, and rhythm), then
    converts that directly to MusicXML — bypassing Audiveris' MusicXML export
    which has two major bugs:

    1. Flattens chords — ~98% of detected chord groupings are lost
    2. Inverts pitches for multi-notehead chords (87-97% across our test pages)

    Set ``use_omr_direct=False`` in the constructor to fall back to Audiveris'
    own MusicXML export (not recommended).
    """

    name = "audiveris"

    def __init__(self, binary: str = "audiveris", use_omr_direct: bool = True):
        self.binary = binary
        self.use_omr_direct = use_omr_direct

    def is_available(self) -> bool:
        return shutil.which(self.binary) is not None

    def transcribe(self, pdf_path: Path, output_dir: Path) -> list[Path]:
        if not self.is_available():
            raise OMRError(
                f"Audiveris binary '{self.binary}' not found on PATH. "
                "Install Audiveris or set CHOIRREADER_AUDIVERIS_BIN."
            )

        output_dir.mkdir(parents=True, exist_ok=True)

        # Render PDF to PNG (with defensive downscaling for Audiveris' 20M-pixel cap)
        pages = _pdf_to_pngs(pdf_path, output_dir / "_pages")

        results: list[Path] = []
        failures: list[str] = []
        for i, page in enumerate(pages, start=1):
            page_out = output_dir / f"page_{i:03d}"
            page_out.mkdir(parents=True, exist_ok=True)

            # Run Audiveris to produce the .omr file (regardless of which output we use)
            cmd = [
                self.binary,
                "-batch",
                "-transcribe",
                "-output",
                str(page_out),
                str(page),
            ]
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=600
            )
            if proc.returncode != 0:
                failures.append(
                    f"page {i} ({page.name}): {proc.stderr[-300:].strip()}"
                )
                continue

            if self.use_omr_direct:
                # Convert .omr directly to MusicXML (bypasses export bugs)
                mxl = self._convert_omr_to_musicxml(page_out)
            else:
                # Use Audiveris' own MusicXML export (legacy, has bugs)
                mxl = _find_mxl(page_out)

            if mxl is None:
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

    def _convert_omr_to_musicxml(self, page_out: Path) -> Path | None:
        """Convert an .omr file to MusicXML using the direct parser."""
        omr_files = list(page_out.glob("*.omr"))
        if not omr_files:
            log.warning("No .omr file found in %s", page_out)
            return None

        omr_file = omr_files[0]
        mxl_output = page_out / f"{omr_file.stem}.mxl"

        try:
            from .omr_direct import convert_omr_to_musicxml
            stats = convert_omr_to_musicxml(omr_file, mxl_output)
            log.info(
                "OMR-direct converted %s: %d staves, %d measures, %d chords",
                omr_file.name, stats["staves"], stats["measures"], stats["chords_emitted"],
            )
            return mxl_output
        except Exception as e:
            log.warning("OMR-direct conversion failed for %s: %s", omr_file, e)
            return None


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
        return AudiverisBackend(
            binary=config.audiveris_bin,
            use_omr_direct=getattr(config, "use_omr_direct", True),
        )
    raise OMRError(f"Unknown OMR backend: {name!r}")
