# Offline AI Patient Report Generator

This tool reads a care-management JSON export, creates a patient-centred view of the records, asks an **offline local Ollama model** for an evidence-based narrative summary, and saves a professional PDF for every patient in `Saved/`.

It does not use cloud APIs or require API keys. The generated AI text is an administrative summary, not medical advice or a diagnosis.

## What it extracts

- Patient identity and supplied demographic/profile fields
- Care team, providers, support workers, and their roles
- Related shifts (date, time, worker, and status where available)
- Non-deleted daily summaries, including activities, mood, and items to know
- Profile details such as medication, allergies, mobility, routines, and contacts
- An Ollama-generated conclusion, documented mood assessment, health-condition summary, and follow-up items

The extractor supports common field-name variations and chooses direct patient references whenever present. Some exports, including the included `test-data.json`, store daily summaries and shifts only at team level. In this case it associates them with the patient’s team and records that fact in the PDF under **Data Linkage Notes**.

## Prerequisites

- Python 3.10 or newer
- [Ollama](https://ollama.com/) installed and running locally
- An Ollama model downloaded locally, for example `llama3.2`

## Setup (Windows PowerShell)

```powershell
cd D:\Descriptive
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
ollama pull llama3.2
ollama serve
```

If Ollama is already running, do not start a second `ollama serve` process. To confirm the model is available, run `ollama list`.

## Generate reports

In a new PowerShell window (activate the virtual environment again if needed), run:

```powershell
python patient_report_generator.py test-data.json --model llama3.2
```

Use any compatible JSON export by replacing `test-data.json` with its path:

```powershell
python patient_report_generator.py C:\Exports\care-export.json --model mistral
```

Optional parameters:

```text
--output-dir Saved       folder to receive PDFs (default: Saved)
--ollama-url URL         Ollama endpoint (default: http://localhost:11434)
--timeout 180            maximum seconds to wait for each patient analysis
--combined               write all patient reports into one PDF
```

Each file is saved as `Saved/<patient_name_or_id>_report_<YYYY-MM-DD>.pdf`. If a report with that name is already present (for example, it is open in a PDF reader), a number such as `_2` is added instead of overwriting it.

## Use the local web page

Install/update the dependencies, then start the local server:

```powershell
pip install -r requirements.txt
python web_app.py
```

Open **http://127.0.0.1:5000** in a browser. Choose the JSON file, enter the installed Ollama model name, and select **Generate reports**. The browser displays every patient's extracted care data and AI summary, embeds each generated PDF, and saves the PDF copies in `Saved/`. The web server binds only to your own computer, so uploaded care data is not exposed to the local network.

## How it works

1. Validates and loads the JSON file.
2. Finds patient/service-user records from common collection names, role values, and flags such as `isSupportedPerson`.
3. Resolves related profiles, care-team records, schedules, shifts, and daily summaries. Direct patient links have priority; team links are a documented fallback.
4. Sends only the current patient’s structured context to `http://localhost:11434/api/generate` using the selected local model.
5. Renders a styled PDF containing extracted facts plus the model’s conservative narrative.

## Troubleshooting

**“Cannot start report generation” and connection error** — Start Ollama with `ollama serve`, then verify `http://localhost:11434/api/tags` is reachable. Ensure `--ollama-url` matches your local installation.

**Model not found** — Run `ollama list` and pass the exact local name using `--model`, or download one with `ollama pull <model-name>`.

**Malformed JSON** — Validate the export JSON and ensure the path points to the intended file.

**No patient records found** — The JSON needs a patient collection, a patient-like role (for example `central-person`), or a flag such as `isSupportedPerson: true`. Extend `PATIENT_MARKERS` in `patient_report_generator.py` if your platform uses different terminology.

**PDF does not include a particular record** — Prefer exports that place a patient ID on shifts and summaries. Team-only records are necessarily an inference if a team contains multiple patients; the generated PDF states when this fallback was used.
