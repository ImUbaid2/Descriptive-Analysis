"""Local browser interface for the offline patient report generator."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, abort, render_template_string, request, send_from_directory

from patient_report_generator import (
    OLLAMA_URL,
    analyse_with_ollama,
    available_report_path,
    extract_reports,
    first_value,
    person_name,
    render_pdf,
)

BASE_DIR = Path(__file__).resolve().parent
SAVED_DIR = BASE_DIR / "Saved"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
app.config["SECRET_KEY"] = "local-only-not-used-for-session-data"
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Patient Report Generator</title><style>
body{font-family:Arial,sans-serif;margin:0;background:#f4f7fa;color:#182433}.wrap{max-width:1100px;margin:auto;padding:32px 20px}
h1,h2,h3{color:#17365D}.panel,.patient{background:#fff;border-radius:10px;padding:24px;margin:18px 0;box-shadow:0 2px 10px #17365d18}.upload{border:2px dashed #7893b2;text-align:center}.button{background:#17365D;color:#fff;border:0;border-radius:6px;padding:11px 18px;font-size:16px;cursor:pointer}.button:hover{background:#0e2848}.notice{padding:12px;border-radius:6px;background:#fce8e6;color:#8a1c13}.meta{color:#5b6775}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:15px}.card{background:#f4f7fa;border-left:4px solid #17365D;padding:14px}.card h3{margin-top:0;font-size:16px}.data{white-space:pre-line;line-height:1.5}.pdf{width:100%;height:600px;border:1px solid #d5dee8;border-radius:6px;margin-top:14px}label{display:block;margin:14px 0 6px;font-weight:bold}input{max-width:100%}details{margin-top:12px}summary{cursor:pointer;font-weight:bold}
</style></head><body><main class="wrap"><h1>Offline Patient Report Generator</h1><p class="meta">Your JSON and AI analysis stay on this computer. Reports are saved to <code>Saved/</code>.</p>
<section class="panel upload"><h2>Upload care-data JSON</h2><form method="post" enctype="multipart/form-data"><label for="file">JSON export (max 10 MB)</label><input id="file" name="file" type="file" accept=".json,application/json" required><label for="model">Installed Ollama model</label><input id="model" name="model" value="{{ model }}" required><p><button class="button" type="submit">Generate reports</button></p></form></section>
{% if error %}<p class="notice">{{ error }}</p>{% endif %}
{% if generated %}<section class="panel"><h2>Generated {{ generated|length }} report(s)</h2><p class="meta">PDF files have been saved in the local <code>Saved</code> folder.</p></section>{% endif %}
{% for item in generated %}<article class="patient"><h2>{{ item.name }}</h2><p><a class="button" href="{{ url_for('saved_file', filename=item.filename) }}" target="_blank">Open saved PDF</a></p><div class="grid">
<section class="card"><h3>Conclusion</h3><div class="data">{{ item.analysis.conclusion }}</div></section><section class="card"><h3>Mood / mental state</h3><div class="data">{{ item.analysis.mood_assessment }}</div></section><section class="card"><h3>Documented health conditions</h3><div class="data">{{ item.analysis.health_condition_summary }}</div></section><section class="card"><h3>Notable follow-ups</h3><div class="data">{{ item.analysis.notable_follow_ups }}</div></section></div>
<details><summary>Extracted care data</summary><div class="grid"><section><h3>Care team</h3><div class="data">{{ item.care_team }}</div></section><section><h3>Shift history</h3><div class="data">{{ item.shifts }}</div></section><section><h3>Daily summaries</h3><div class="data">{{ item.summaries }}</div></section></div></details>
<iframe class="pdf" title="{{ item.name }} PDF report" src="{{ url_for('saved_file', filename=item.filename) }}"></iframe></article>{% endfor %}
</main></body></html>"""


def display_lines(rows: list[dict], fields: tuple[str, ...]) -> str:
    if not rows:
        return "No records found."
    result = []
    for row in rows:
        values = [f"{field}: {first_value(row, (field,))}" for field in fields if first_value(row, (field,)) not in (None, "")]
        result.append("; ".join(values) or "No relevant fields recorded")
    return "\n".join(result)


@app.route("/", methods=["GET", "POST"])
def index():
    generated: list[dict] = []
    model = request.form.get("model", "llama3.2")
    if request.method == "POST":
        upload = request.files.get("file")
        if not upload or not upload.filename:
            return render_template_string(PAGE, generated=[], model=model, error="Choose a JSON file first.")
        try:
            data = json.loads(upload.read().decode("utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("The top-level JSON value must be an object.")
            requests.get(f"{OLLAMA_URL}/api/tags", timeout=5).raise_for_status()
            reports = extract_reports(data)
            SAVED_DIR.mkdir(parents=True, exist_ok=True)
            date_tag = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            for report in reports:
                analysis = analyse_with_ollama(report, model, OLLAMA_URL, 180)
                pdf_path = available_report_path(SAVED_DIR, person_name(report.patient), date_tag)
                filename = pdf_path.name
                render_pdf(report, analysis, pdf_path)
                generated.append({
                    "name": person_name(report.patient), "filename": filename, "analysis": analysis,
                    "care_team": display_lines(report.care_team, ("fullName", "role", "email")),
                    "shifts": display_lines(report.shifts, ("startTime", "finishTime", "workerMemberId", "status")),
                    "summaries": display_lines(report.summaries, ("createdAt", "mood", "activities", "needToKnow")),
                })
        except UnicodeDecodeError:
            return render_template_string(PAGE, generated=[], model=model, error="The file must be UTF-8 encoded JSON.")
        except (json.JSONDecodeError, ValueError) as error:
            return render_template_string(PAGE, generated=[], model=model, error=f"Invalid or unsupported JSON: {error}")
        except requests.RequestException:
            return render_template_string(PAGE, generated=[], model=model, error="Ollama is not reachable. Start Ollama, then try again.")
        except Exception as error:  # Keep a failed upload from stopping the local web server.
            app.logger.exception("Report generation failed")
            return render_template_string(PAGE, generated=[], model=model, error=f"Report generation failed: {error}")
    return render_template_string(PAGE, generated=generated, model=model, error=None)


@app.route("/saved/<path:filename>")
def saved_file(filename: str):
    if Path(filename).name != filename or not filename.lower().endswith(".pdf"):
        abort(404)
    return send_from_directory(SAVED_DIR, filename, mimetype="application/pdf")


@app.errorhandler(413)
def file_too_large(_error):
    return render_template_string(PAGE, generated=[], model="llama3.2", error="The uploaded JSON is larger than 10 MB."), 413


if __name__ == "__main__":
    # Localhost prevents uploaded care data being exposed to the network.
    app.run(host="127.0.0.1", port=5000, debug=False)
