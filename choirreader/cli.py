"""Command-line interface for ChoirReader.

Headless usage:
    choirreader transcribe input.pdf -o output/ --max-iterations 3
    choirreader transcribe input.pdf --no-vision
    choirreader serve            # start the web UI
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import Config
from .correction import transcribe_pdf
from .musicxml import to_midi


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="choirreader",
        description="OMR for choral sheet music with an AI-assisted correction loop.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # transcribe
    t = sub.add_parser("transcribe", help="Transcribe a PDF to MusicXML/MIDI")
    t.add_argument("input", type=Path, help="Input PDF file")
    t.add_argument("-o", "--output", type=Path, default=Path("output"),
                   help="Output directory (default: ./output)")
    t.add_argument("--max-iterations", type=int, default=None,
                   help="Correction-loop passes (default: 3)")
    t.add_argument("--no-vision", action="store_true",
                   help="Skip the vision correction loop entirely")
    t.add_argument("--vision-model", type=str, default=None,
                   help="Ollama model for comparison (default: minimax-m3:cloud)")
    t.add_argument("--omr-backend", type=str, default=None,
                   help="OMR engine (default: audiveris)")
    t.add_argument("--renderer", type=str, default=None,
                   help="MusicXML->image renderer: musescore or lilypond")
    t.add_argument("--no-midi", action="store_true",
                   help="Do not also emit MIDI")
    t.add_argument("--verbose", action="store_true", help="Verbose logging")

    # serve
    s = sub.add_parser("serve", help="Start the web UI")
    s.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)")
    s.add_argument("--port", type=int, default=8000, help="Bind port (default 8000)")
    s.add_argument("--max-iterations", type=int, default=None,
                   help="Correction-loop passes (default: 3)")
    s.add_argument("--vision-model", type=str, default=None,
                   help="Ollama model for comparison")

    return p


def _cmd_transcribe(args) -> int:
    cfg = Config.from_kwargs(
        max_iterations=args.max_iterations,
        vision_model=args.vision_model,
        omr_backend=args.omr_backend,
        renderer=args.renderer,
    )
    if args.no_vision:
        cfg.vision_model = ""
        cfg.max_iterations = 0

    if not args.input.exists():
        print(f"error: input file not found: {args.input}", file=sys.stderr)
        return 1

    args.output.mkdir(parents=True, exist_ok=True)
    print(f"Transcribing {args.input} -> {args.output}")
    print(f"  backend={cfg.omr_backend} renderer={cfg.renderer} "
          f"max_iterations={cfg.max_iterations} vision={cfg.vision_model or 'off'}")

    results = transcribe_pdf(args.input, args.output, cfg)

    for r in results:
        status = "ok" if r.musicxml else "no music"
        applied = r.total_applied
        print(f"  page {r.page_number}: {status} "
              f"({applied} corrections applied across {len(r.iterations)} iterations)")
        if r.musicxml and not args.no_midi:
            midi = r.musicxml.with_suffix(".mid")
            to_midi(r.musicxml, midi)
            print(f"    -> {midi.name}")

    print("Done.")
    return 0


def _cmd_serve(args) -> int:
    from .webapp import create_app

    cfg = Config.from_kwargs(
        max_iterations=args.max_iterations,
        vision_model=args.vision_model,
    )
    app = create_app(cfg)
    print(f"Serving ChoirReader on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.verbose:
        logging.basicConfig(level=logging.INFO)
    else:
        logging.basicConfig(level=logging.WARNING)

    if args.command == "transcribe":
        return _cmd_transcribe(args)
    if args.command == "serve":
        return _cmd_serve(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
