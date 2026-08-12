#!/usr/bin/env python3
"""Create offline, Ollama-assisted patient care reports from JSON exports."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from anonymizer import anonymise_export

LOG = logging.getLogger("patient_report_generator")
OLLAMA_URL = "http://localhost:11434"

PATIENT_MARKERS = ("patient", "service user", "service_user", "supported person", "supported_person", "central person", "central_person")
PERSON_ID_FIELDS = ("patientId", "patient_id", "serviceUserId", "service_user_id", "supportedPersonId", "supported_person_id", "memberId", "member_id")
TEAM_FIELDS = ("teamId", "team_id")


@dataclass
class PatientReport:
    patient: dict[str, Any]
    care_team: list[dict[str, Any]] = field(default_factory=list)
    shifts: list[dict[str, Any]] = field(default_factory=list)
    summaries: list[dict[str, Any]] = field(default_factory=list)
    profile_details: list[dict[str, Any]] = field(default_factory=list)
    linkage_notes: list[str] = field(default_factory=list)
    export_overview: list[tuple[str, str]] = field(default_factory=list)


def records(data: dict[str, Any], *names: str) -> list[dict[str, Any]]:
    """Return records from case-insensitive top-level collection names."""
    wanted = {name.lower() for name in names}
    for key, value in data.items():
        if key.lower() in wanted and isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def first_value(row: dict[str, Any], keys: Iterable[str], default: Any = None) -> Any:
    lookup = {str(k).lower(): v for k, v in row.items()}
    for key in keys:
        value = lookup.get(key.lower())
        if value not in (None, ""):
            return value
    return default


def person_name(row: dict[str, Any]) -> str:
    full = first_value(row, ("fullName", "full_name", "name", "displayName", "display_name"))
    if full:
        return str(full)
    joined = " ".join(str(x) for x in (first_value(row, ("firstName", "firstname", "first_name"), ""), first_value(row, ("lastName", "surname", "last_name"), "")) if x)
    return joined or str(first_value(row, ("id", "memberId", "patientId"), "Unnamed patient"))


def row_person_ids(row: dict[str, Any]) -> set[str]:
    values = {str(row[key]) for key in row if key.lower() in {x.lower() for x in PERSON_ID_FIELDS} and row[key]}
    for key in ("patient", "serviceUser", "supportedPerson", "member"):
        value = row.get(key)
        if isinstance(value, dict) and value.get("id"):
            values.add(str(value["id"]))
    return values


def row_team_id(row: dict[str, Any]) -> str | None:
    return str(first_value(row, TEAM_FIELDS)) if first_value(row, TEAM_FIELDS) else None


def find_patients(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Find patient records across common export schemas."""
    candidates: list[dict[str, Any]] = []
    for collection, value in data.items():
        if not isinstance(value, list):
            continue
        collection_is_patient = any(marker.replace(" ", "") in collection.lower().replace("_", "") for marker in PATIENT_MARKERS)
        for row in value:
            if not isinstance(row, dict):
                continue
            role = str(first_value(row, ("role", "type", "memberType", "member_type"), "")).lower().replace("-", " ").replace("_", " ")
            flag = bool(first_value(row, ("isSupportedPerson", "isPatient", "isServiceUser", "is_supported_person"), False))
            if collection_is_patient or flag or any(marker in role for marker in PATIENT_MARKERS):
                candidates.append(row)
    # Preserve input order while eliminating duplicates by stable id/object identity.
    seen: set[str] = set()
    return [x for x in candidates if not (str(x.get("id", id(x))) in seen or seen.add(str(x.get("id", id(x)))))]


def export_data_overview(data: dict[str, Any]) -> list[tuple[str, str]]:
    """Return descriptive counts for the source export and its member roles."""
    members = records(data, "Member", "members")
    role_counts = Counter(str(first_value(member, ("role", "memberRole", "member_role"), "Role not recorded")) for member in members)
    overview = [
        ("Teams", str(len(records(data, "Team", "teams")))),
        ("Members", str(len(members))),
    ]
    overview.extend((f"Members — {role}", str(count)) for role, count in sorted(role_counts.items(), key=lambda item: item[0].lower()))
    overview.extend([
        ("Invites", str(len(records(data, "Invite", "invites")))),
        ("Profiles", str(len(records(data, "Profile", "profiles")))),
        ("Profile definitions", str(len(records(data, "ProfileDefinition", "profileDefinitions", "profile_definitions")))),
        ("Daily summaries", str(len(records(data, "DailySummary", "dailySummaries", "daily_summaries")))),
        ("Daily summary versions", str(len(records(data, "DailySummaryVersions", "dailySummaryVersions", "daily_summary_versions")))),
        ("Team schedules", str(len(records(data, "TeamSchedule", "teamSchedules", "team_schedules")))),
        ("Shifts", str(len(records(data, "Shift", "shifts")))),
    ])
    return overview


def extract_reports(data: dict[str, Any]) -> list[PatientReport]:
    """Build patient-centred views, preferring direct IDs over team inference."""
    patients = find_patients(data)
    if not patients:
        raise ValueError("No patient/service-user records were found. Check role or patient flags in the JSON.")

    members = records(data, "Member", "members", "caregivers", "providers", "workers")
    profiles = records(data, "Profile", "profiles")
    definitions = records(data, "ProfileDefinition", "profileDefinitions", "profile_definitions")
    schedules = records(data, "TeamSchedule", "teamSchedules", "team_schedules")
    raw_shifts = records(data, "Shift", "shifts")
    summaries = [x for x in records(data, "DailySummary", "dailySummaries", "daily_summaries", "notes") if not x.get("deleted", False)]
    schedule_by_id = {str(x.get("id")): x for x in schedules if x.get("id")}
    teams = records(data, "Team", "teams")
    team_by_group = {
        str(group): str(team["id"])
        for team in teams if team.get("id")
        for group in (team.get("memberGroups", []) + team.get("adminGroups", []))
    }

    # Enrich shifts with their worker/team information where the export separates schedules.
    shifts: list[dict[str, Any]] = []
    for shift in raw_shifts:
        item = dict(shift)
        schedule = schedule_by_id.get(str(first_value(shift, ("teamScheduleId", "scheduleId", "schedule_id"), "")))
        if schedule:
            schedule_team = row_team_id(schedule) or next((team_by_group.get(str(group)) for group in schedule.get("memberGroups", []) + schedule.get("adminGroups", []) if team_by_group.get(str(group))), None)
            if schedule_team:
                item["teamId"] = schedule_team
            item.setdefault("workerMemberId", first_value(schedule, ("memberId", "workerId", "carerId")))
        shifts.append(item)

    reports: list[PatientReport] = []
    overview = export_data_overview(data)
    for patient in patients:
        patient_id = str(patient.get("id", ""))
        team_id = row_team_id(patient)
        patient_profiles = [p for p in profiles if str(first_value(p, ("memberId", "patientId", "serviceUserId"), "")) == patient_id]
        profile_ids = {str(p.get("id")) for p in patient_profiles}
        details = [
            {"category": "Profile", "name": "Biography", "description": profile["bio"], "sourceCollection": "Profile", "sourceId": profile.get("id")}
            for profile in patient_profiles if profile.get("bio")
        ] + [d for d in definitions if str(first_value(d, ("profileId", "profile_id"), "")) in profile_ids]

        direct_care_team = [m for m in members if patient_id in row_person_ids(m) and str(m.get("id")) != patient_id]
        team_members = [m for m in members if team_id and row_team_id(m) == team_id and str(m.get("id")) != patient_id]
        care_team = direct_care_team or team_members
        direct_shifts = [s for s in shifts if patient_id in row_person_ids(s)]
        direct_summaries = [s for s in summaries if patient_id in row_person_ids(s)]
        patient_shifts = direct_shifts or ([s for s in shifts if team_id and row_team_id(s) == team_id])
        patient_summaries = direct_summaries or ([s for s in summaries if team_id and row_team_id(s) == team_id])
        notes: list[str] = []
        if not direct_shifts and patient_shifts:
            notes.append("Shift records are associated by team because the export contains no direct patient reference.")
        if not direct_summaries and patient_summaries:
            notes.append("Daily summaries are associated by team because the export contains no direct patient reference.")
        reports.append(PatientReport(patient, care_team, patient_shifts, patient_summaries, details, notes, overview))
    return reports


def compact_row(row: dict[str, Any], fields: tuple[str, ...]) -> str:
    parts = []
    for field in fields:
        value = first_value(row, (field,))
        if value not in (None, "", [], {}):
            parts.append(f"{field}: {value}")
    return "; ".join(parts) or "No data recorded"


def llm_context(report: PatientReport) -> dict[str, Any]:
    return {
        "patient": report.patient,
        "profile_details": report.profile_details,
        "care_team": [{"name": person_name(x), "role": first_value(x, ("role", "jobTitle", "type")), "status": "inactive" if x.get("inactive") else "active"} for x in report.care_team],
        "shifts": [{"start": first_value(x, ("startTime", "start", "date")), "finish": first_value(x, ("finishTime", "end")), "worker": first_value(x, ("workerMemberId", "workerId", "carerId"))} for x in report.shifts],
        "daily_summaries": [{"date": first_value(x, ("createdAt", "date")), "mood": x.get("mood"), "activities": x.get("activities", x.get("text")), "need_to_know": first_value(x, ("needToKnow", "alerts", "notes"))} for x in report.summaries],
    }


def short_text(value: Any, limit: int = 260) -> str:
    """Make a single concise, report-ready sentence from an exported field."""
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def record_based_analysis(report: PatientReport) -> dict[str, str]:
    """Create useful factual sections when an LLM field is empty or unavailable."""
    name = person_name(report.patient)
    activities = [short_text(first_value(item, ("activities", "text", "notes"))) for item in report.summaries if first_value(item, ("activities", "text", "notes"))]
    conclusion_parts = [f"{name} has {len(report.shifts)} recorded shift(s) and {len(report.summaries)} daily summary entry/entries for this reporting period."]
    if activities:
        conclusion_parts.append(f"Care records note: {activities[-1]}")

    mood_entries = []
    for item in report.summaries:
        mood = short_text(item.get("mood"))
        if mood and mood not in {"-", "Not recorded"}:
            date = short_text(first_value(item, ("createdAt", "date")))
            mood_entries.append(f"{mood}{f' ({date[:10]})' if date else ''}")
    mood = "Recorded mood observations: " + "; ".join(mood_entries) + "." if mood_entries else "No mood observations were recorded in the supplied daily summaries."

    health_entries = []
    bio = first_value(report.patient, ("bio", "medicalHistory", "medical_history", "conditions"))
    if bio:
        health_entries.append(short_text(bio))
    for item in report.profile_details:
        category = str(first_value(item, ("category", "section"), "")).lower()
        if category in {"medical", "health", "mobility", "medication", "allergies"} or (category == "profile" and str(first_value(item, ("name", "label"), "")).lower() == "biography"):
            label = short_text(first_value(item, ("name", "label")))
            description = short_text(first_value(item, ("description", "value", "text")))
            health_entries.append(f"{label}: {description}" if label else description)
    health = "Documented information: " + "; ".join(health_entries) + "." if health_entries else "No documented health-condition information was found in the supplied records."

    follow_ups = [short_text(first_value(item, ("needToKnow", "need_to_know", "alerts"))) for item in report.summaries if first_value(item, ("needToKnow", "need_to_know", "alerts")) not in (None, "", "-")]
    return {
        "conclusion": " ".join(conclusion_parts),
        "mood_assessment": mood,
        "health_condition_summary": health,
        "notable_follow_ups": "Recorded follow-up items: " + "; ".join(follow_ups) + "." if follow_ups else "No follow-up items were recorded.",
    }


def needs_fallback(value: Any) -> bool:
    return not str(value or "").strip() or str(value).strip().lower() in {"not recorded", "no information available", "n/a", "none"}


def analyse_with_ollama(report: PatientReport, model: str, url: str, timeout: int) -> dict[str, str]:
    """Request conservative, evidence-only narrative sections from local Ollama."""
    prompt = """You are preparing a short care-record summary. Use ONLY the supplied data. Do not diagnose, invent facts, or give medical advice. Clearly distinguish documented conditions from observations. For mood_assessment, quote or closely preserve the mood wording in daily_summaries (for example, 'Cheerful and chatty') rather than returning 'Not recorded' when mood values are supplied. Return valid JSON only with exactly these string keys: conclusion, mood_assessment, health_condition_summary, notable_follow_ups.\n\nDATA:\n""" + json.dumps(llm_context(report), ensure_ascii=False, default=str)
    fallback = record_based_analysis(report)
    try:
        response = requests.post(f"{url.rstrip('/')}/api/generate", json={"model": model, "prompt": prompt, "format": "json", "stream": False, "options": {"temperature": 0.1}}, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        answer = json.loads(payload["response"])
        return {key: fallback[key] if needs_fallback(answer.get(key)) else str(answer[key]) for key in fallback}
    except (requests.RequestException, KeyError, ValueError, json.JSONDecodeError) as error:
        LOG.warning("Ollama analysis unavailable for %s: %s", person_name(report.patient), error)
        return fallback


def esc(value: Any) -> str:
    from xml.sax.saxutils import escape
    return escape(str(value if value not in (None, "") else "Not recorded"))


def section(story: list, title: str, text: str, styles: dict, references: str | None = None) -> None:
    story += [Spacer(1, 8), Paragraph(esc(title), styles["Section"]), Paragraph(esc(text), styles["BodyText"])]
    if references:
        story.append(Paragraph("<b>JSON source references:</b> " + esc(references), styles["Reference"]))


def report_styles() -> dict:
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle("TitleCenter", parent=styles["Title"], alignment=TA_CENTER, textColor=colors.HexColor("#17365D")))
    styles.add(ParagraphStyle("Section", parent=styles["Heading2"], textColor=colors.HexColor("#17365D"), spaceBefore=10))
    styles.add(ParagraphStyle("Reference", parent=styles["BodyText"], fontSize=8, leading=10, textColor=colors.HexColor("#52687C"), leftIndent=8, spaceBefore=3))
    return styles


def json_record_reference(collection: str, record: dict[str, Any], fields: tuple[str, ...]) -> str:
    """Describe the precise JSON record/fields used without exposing raw text."""
    identifier = first_value(record, ("id", "sourceId"), "record without ID")
    date = first_value(record, ("createdAt", "date", "startTime"))
    field_list = ", ".join(field for field in fields if first_value(record, (field,)) not in (None, "")) or "linked record"
    date_part = f", date: {str(date)[:10]}" if date else ""
    return f"{collection} (id: {identifier}{date_part}; fields: {field_list})"


def reference_list(references: list[str], maximum: int = 4) -> str:
    if not references:
        return "No matching source records were available."
    visible = references[:maximum]
    suffix = f"; plus {len(references) - maximum} additional matching record(s)" if len(references) > maximum else ""
    return "; ".join(visible) + suffix + "."


def analysis_json_references(report: PatientReport) -> dict[str, str]:
    """Build audit-friendly provenance for the AI overview sections."""
    summary_refs = [json_record_reference("DailySummary", row, ("activities", "mood", "needToKnow")) for row in report.summaries]
    shift_refs = [json_record_reference("Shift", row, ("startTime", "finishTime", "workerMemberId")) for row in report.shifts]
    mood_refs = [json_record_reference("DailySummary", row, ("mood",)) for row in report.summaries if row.get("mood") not in (None, "", "-")]
    health_refs = [json_record_reference(str(row.get("sourceCollection", "ProfileDefinition")), row, ("category", "name", "description")) for row in report.profile_details if str(first_value(row, ("category", "section"), "")).lower() in {"medical", "health", "mobility", "medication", "allergies", "profile"}]
    follow_up_refs = [json_record_reference("DailySummary", row, ("needToKnow",)) for row in report.summaries if first_value(row, ("needToKnow", "need_to_know", "alerts")) not in (None, "", "-")]
    linkage_refs = [json_record_reference("Member", report.patient, ("teamId",))]
    linkage_refs.extend(json_record_reference("Shift", row, ("teamScheduleId", "teamId", "workerMemberId")) for row in report.shifts[:2])
    linkage_refs.extend(json_record_reference("DailySummary", row, ("teamId",)) for row in report.summaries[:2])
    return {
        "conclusion": reference_list(summary_refs[:3] + shift_refs[:1]),
        "mood_assessment": reference_list(mood_refs),
        "health_condition_summary": reference_list(health_refs),
        "notable_follow_ups": reference_list(follow_up_refs),
        "data_linkage": reference_list(linkage_refs) if report.linkage_notes else "No inferred team linkage was used.",
    }


def report_story(report: PatientReport, analysis: dict[str, str], styles: dict) -> list:
    story: list = [Paragraph("Patient Care Report", styles["TitleCenter"]), Paragraph(esc(person_name(report.patient)), styles["Heading2"]), Paragraph(f"Generated: {datetime.now().strftime('%d %B %Y, %H:%M')}", styles["BodyText"])]
    patient_items = [["Field", "Value"]] + [[esc(k), esc(v)] for k, v in report.patient.items() if k not in {"owners", "memberGroups", "adminGroups", "profileImage"}]
    table = Table(patient_items, colWidths=[4.2*cm, 12.5*cm], repeatRows=1)
    table.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,0), colors.HexColor("#17365D")), ("TEXTCOLOR", (0,0), (-1,0), colors.white), ("GRID", (0,0), (-1,-1), .25, colors.lightgrey), ("VALIGN", (0,0), (-1,-1), "TOP"), ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"), ("FONTSIZE", (0,0), (-1,-1), 8), ("LEFTPADDING", (0,0), (-1,-1), 5), ("RIGHTPADDING", (0,0), (-1,-1), 5)]))
    story += [Spacer(1, 8), Paragraph("Patient Information", styles["Section"]), table]
    overview_items = [["Source JSON record type", "Count"]] + [[esc(label), esc(count)] for label, count in report.export_overview]
    overview_table = Table(overview_items, colWidths=[12.0*cm, 4.7*cm], repeatRows=1)
    overview_table.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,0), colors.HexColor("#17365D")), ("TEXTCOLOR", (0,0), (-1,0), colors.white), ("GRID", (0,0), (-1,-1), .25, colors.lightgrey), ("VALIGN", (0,0), (-1,-1), "TOP"), ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"), ("FONTSIZE", (0,0), (-1,-1), 9), ("ALIGN", (1,1), (1,-1), "RIGHT"), ("LEFTPADDING", (0,0), (-1,-1), 5), ("RIGHTPADDING", (0,0), (-1,-1), 5)]))
    story += [Spacer(1, 8), Paragraph("Export Data Overview", styles["Section"]), overview_table]
    section(story, "Care Team / Providers", "\n".join(f"{person_name(x)} — {first_value(x, ('role', 'jobTitle', 'type'), 'role not recorded')}" for x in report.care_team) or "No care-team records found.", styles)
    section(story, "Profile and Care Information", "\n".join(compact_row(x, ("category", "name", "description")) for x in report.profile_details) or "No profile details found.", styles)
    section(story, "Shift History", "\n".join(compact_row(x, ("startTime", "finishTime", "workerMemberId", "status")) for x in report.shifts) or "No shift records found.", styles)
    section(story, "Daily Summaries", "\n".join(compact_row(x, ("createdAt", "mood", "activities", "needToKnow")) for x in report.summaries) or "No daily summaries found.", styles)
    story += [PageBreak(), Paragraph("AI-Generated Care Overview", styles["TitleCenter"])]
    references = analysis_json_references(report)
    section(story, "Conclusion", analysis["conclusion"], styles, references["conclusion"])
    section(story, "Mood / Mental State", analysis["mood_assessment"], styles, references["mood_assessment"])
    section(story, "Documented Health Conditions", analysis["health_condition_summary"], styles, references["health_condition_summary"])
    section(story, "Notable Follow-ups", analysis["notable_follow_ups"], styles, references["notable_follow_ups"])
    if report.linkage_notes:
        section(story, "Data Linkage Notes", " ".join(report.linkage_notes), styles, references["data_linkage"])
    section(story, "Important", "This report is an administrative summary of supplied care records. It is not a diagnosis, clinical assessment, or substitute for professional medical judgement.", styles)
    return story


def render_pdf(report: PatientReport, analysis: dict[str, str], destination: Path) -> None:
    doc = SimpleDocTemplate(str(destination), pagesize=A4, rightMargin=1.5*cm, leftMargin=1.5*cm, topMargin=1.5*cm, bottomMargin=1.5*cm)
    doc.build(report_story(report, analysis, report_styles()))


def render_combined_pdf(reports_and_analysis: list[tuple[PatientReport, dict[str, str]]], destination: Path) -> None:
    """Render every patient as a separate section in one document."""
    doc = SimpleDocTemplate(str(destination), pagesize=A4, rightMargin=1.5*cm, leftMargin=1.5*cm, topMargin=1.5*cm, bottomMargin=1.5*cm)
    styles = report_styles()
    story: list = []
    for index, (report, analysis) in enumerate(reports_and_analysis):
        if index:
            story.append(PageBreak())
        story.extend(report_story(report, analysis, styles))
    doc.build(story)


def safe_filename(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "unknown_patient"


def available_report_path(output_dir: Path, patient_name: str, date_tag: str) -> Path:
    """Choose a new report name so a PDF open in Windows is never overwritten."""
    base = f"{safe_filename(patient_name)}_report_{date_tag}"
    candidate = output_dir / f"{base}.pdf"
    index = 2
    while candidate.exists():
        candidate = output_dir / f"{base}_{index}.pdf"
        index += 1
    return candidate


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate offline Ollama-assisted patient care PDFs from JSON.")
    parser.add_argument("input_json", type=Path, help="Path to the JSON export")
    parser.add_argument("--model", default="llama3.2", help="Installed Ollama model name (default: llama3.2)")
    parser.add_argument("--ollama-url", default=OLLAMA_URL, help="Local Ollama API URL")
    parser.add_argument("--output-dir", type=Path, default=Path("Saved"), help="Folder for generated PDFs")
    parser.add_argument("--combined", action="store_true", help="Write a single combined PDF instead of one PDF per patient")
    parser.add_argument("--timeout", type=int, default=180, help="Ollama request timeout in seconds")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        data = json.loads(args.input_json.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict): raise ValueError("Top-level JSON must be an object.")
        data = anonymise_export(data)
        requests.get(f"{args.ollama_url.rstrip('/')}/api/tags", timeout=5).raise_for_status()
        reports = extract_reports(data)
    except (OSError, json.JSONDecodeError, ValueError, requests.RequestException) as error:
        LOG.error("Cannot start report generation: %s", error)
        return 1
    args.output_dir.mkdir(parents=True, exist_ok=True)
    date_tag = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if args.combined:
        analysed = [(report, analyse_with_ollama(report, args.model, args.ollama_url, args.timeout)) for report in reports]
        output = args.output_dir / f"combined_patient_reports_{date_tag}.pdf"
        render_combined_pdf(analysed, output)
        LOG.info("Saved %s", output)
        return 0
    for report in reports:
        analysis = analyse_with_ollama(report, args.model, args.ollama_url, args.timeout)
        output = available_report_path(args.output_dir, person_name(report.patient), date_tag)
        render_pdf(report, analysis, output)
        LOG.info("Saved %s", output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
