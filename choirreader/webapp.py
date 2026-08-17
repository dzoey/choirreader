"""Flask web UI for ChoirReader.

A minimal single-page app: upload a PDF, run the correction loop, download
the resulting MusicXML/MIDI. Server-rendered templates, no SPA build step.
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path

from flask import Flask, render_template, request, send_file, jsonify

from .config import Config
from .correction import transcribe_pdf
from .musicxml import to_midi

# In-memory job store (single-user local tool; no DB needed).
_JOBS: dict[str, dict] = {}


def create_app(config: Config | None = None) -> Flask:
    config = config or Config()
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB uploads
    app.config["CHOIRREADER_CONFIG"] = config

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/api/transcribe", methods=["POST"])
    def transcribe():
        if "file" not in request.files:
            return jsonify({"error": "no file uploaded"}), 400
        f = request.files["file"]
        if not f.filename.lower().endswith(".pdf"):
            return jsonify({"error": "only PDF files are supported"}), 400

        job_id = uuid.uuid4().hex
        work_dir = Path(app.config["CHOIRREADER_CONFIG"].work_dir or "/tmp/choirreader")
        job_dir = work_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        pdf_path = job_dir / f.filename
        f.save(pdf_path)

        max_iter = request.form.get("max_iterations", type=int)
        cfg = Config.from_kwargs(
            max_iterations=max_iter,
            vision_model=app.config["CHOIRREADER_CONFIG"].vision_model,
            omr_backend=app.config["CHOIRREADER_CONFIG"].omr_backend,
            renderer=app.config["CHOIRREADER_CONFIG"].renderer,
        )

        _JOBS[job_id] = {"status": "running", "dir": str(job_dir), "results": []}

        # Run in a background thread so the request returns immediately.
        thread = threading.Thread(
            target=_run_job, args=(job_id, pdf_path, job_dir, cfg), daemon=True
        )
        thread.start()

        return jsonify({"job_id": job_id})

    @app.route("/api/status/<job_id>")
    def status(job_id):
        job = _JOBS.get(job_id)
        if job is None:
            return jsonify({"error": "unknown job"}), 404
        return jsonify(job)

    @app.route("/api/download/<job_id>/<path:filename>")
    def download(job_id, filename):
        job = _JOBS.get(job_id)
        if job is None:
            return jsonify({"error": "unknown job"}), 404
        # Prevent path traversal.
        safe = Path(filename).name
        path = Path(job["dir"]) / safe
        if not path.exists():
            return jsonify({"error": "file not found"}), 404
        return send_file(path, as_attachment=True)

    return app


def _run_job(job_id: str, pdf_path: Path, job_dir: Path, cfg: Config) -> None:
    """Run the transcription + correction loop in the background."""
    job = _JOBS[job_id]
    try:
        results = transcribe_pdf(pdf_path, job_dir, cfg)
        files = []
        for r in results:
            if r.musicxml is None:
                continue
            midi = r.musicxml.with_suffix(".mid")
            to_midi(r.musicxml, midi)
            files.append({
                "page": r.page_number,
                "musicxml": r.musicxml.name,
                "midi": midi.name,
                "corrections": r.total_applied,
            })
        job["status"] = "done"
        job["results"] = files
    except Exception as e:  # noqa: BLE001 — surface any failure to the UI
        job["status"] = "error"
        job["error"] = str(e)
