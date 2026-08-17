"""Configuration for ChoirReader.

Settings are read from (in order of precedence):
1. CLI flags / explicit kwargs
2. Environment variables (CHOIRREADER_*)
3. Defaults defined here

There is intentionally no config file yet — the surface is small enough that
CLI flags + env vars cover it. Add a TOML/YAML config when it grows.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(f"CHOIRREADER_{name}", default)


@dataclass
class Config:
    # Correction loop
    max_iterations: int = 3

    # OMR backend: "audiveris" (only one implemented for now)
    omr_backend: str = "audiveris"

    # Vision model for the compare/fix loop (Ollama model name)
    vision_model: str = "minimax-m3:cloud"

    # Ollama endpoint
    ollama_url: str = "http://localhost:11434"

    # MusicXML -> image renderer: "musescore" or "lilypond"
    # Default is lilypond: MuseScore's headless render silently fails on MXL
    # (Qt QApplication init issue), while lilypond is fully scriptable.
    renderer: str = "lilypond"

    # Paths to external binaries (empty = rely on PATH)
    audiveris_bin: str = "audiveris"
    musescore_bin: str = "musescore"
    lilypond_bin: str = "lilypond"

    # Working directory for intermediate files (None = system temp)
    work_dir: str | None = None

    def __post_init__(self) -> None:
        # Coerce env-var overrides
        self.max_iterations = int(_env("MAX_ITERATIONS", str(self.max_iterations)))
        self.omr_backend = _env("OMR_BACKEND", self.omr_backend)
        self.vision_model = _env("VISION_MODEL", self.vision_model)
        self.ollama_url = _env("OLLAMA_URL", self.ollama_url)
        self.renderer = _env("RENDERER", self.renderer)
        self.audiveris_bin = _env("AUDIVERIS_BIN", self.audiveris_bin)
        self.musescore_bin = _env("MUSESCORE_BIN", self.musescore_bin)
        self.lilypond_bin = _env("LILYPOND_BIN", self.lilypond_bin)
        if _env("WORK_DIR", ""):
            self.work_dir = _env("WORK_DIR", "")

    @classmethod
    def from_kwargs(cls, **kwargs) -> "Config":
        """Build a Config, letting explicit kwargs override env/defaults."""
        cfg = cls()
        for k, v in kwargs.items():
            if v is not None and hasattr(cfg, k):
                setattr(cfg, k, v)
        return cfg
