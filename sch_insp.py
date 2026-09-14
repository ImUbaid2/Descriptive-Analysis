"""
inCharge Stage 1: Dataset Schema + Entity Discovery

Produces:
  dataset_report.html  Human-readable comprehensive report, including ERDs
  dataset_report.json   Machine-readable discovery report

Usage:
  python sch_insp.py anonymised_incharge_data.json
"""

import argparse
import json
import html
from collections import defaultdict
from pathlib import Path


COLLECTION_HINTS = {
    "teams": "Care teams and team-level configuration.",
    "members": "People associated with teams, including supported people and staff.",
    "profiles": "Supported-person profile records.",
    "profileDefinitions": "Structured profile information and definitions.",
    "dailySummaries": "Day-to-day care and activity records.",
    "dailySummaryVersions": "Historical versions of daily summaries.",
    "teamSchedules": "Planned support schedules for members.",
    "shifts": "Individual planned support shifts.",
}

PII_FIELDS = {
    "firstname", "firstName", "surname", "lastName", "fullName",
    "preferredName", "email", "phone", "telephone", "dob", "dateOfBirth",
}

NARRATIVE_FIELDS = {
    "bio", "activities", "description", "needToKnow", "mood", "changeNote",
}

TIME_SUFFIXES = ("At", "Time", "Date")
REFERENCE_SUFFIX = "Id"


def value_type(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def field_classification(field):
    if field in PII_FIELDS or field.lower() in {x.lower() for x in PII_FIELDS}:
        return "PII"
    if field in NARRATIVE_FIELDS or field.lower() in {x.lower() for x in NARRATIVE_FIELDS}:
        return "narrative"
    if field == "id":
        return "identifier"
    if field.endswith(REFERENCE_SUFFIX):
        return "reference candidate"
    if field.endswith(TIME_SUFFIXES) or field == "weekNumber":
        return "date/time"
    return ""


def inspect_collection(records):
    fields = defaultdict(list)

    for record in records:
        if not isinstance(record, dict):
            continue
        for name, value in record.items():
            fields[name].append(value)

    result = []
    for name, values in fields.items():
        non_null = [v for v in values if v is not None]
        types = sorted({value_type(v) for v in non_null})

        result.append({
            "field": name,
            "type": " | ".join(types) if types else "null",
            "null_count": sum(v is None for v in values),
            "record_count": len(records),
            "classification": field_classification(name),
        })

    return result


def collection_data(env):
    return {
        name: records
        for name, records in env.items()
        if isinstance(records, list)
        and all(isinstance(record, dict) for record in records)
    }


def find_relationships(env_name, env):
    collections = collection_data(env)
    ids = {
        name: {
            str(record.get("id"))
            for record in records
            if record.get("id") is not None
        }
        for name, records in collections.items()
    }

    relationships = []

    for source_name, records in collections.items():
        fields = {key for record in records for key in record}

        for field in fields:
            if field == "postedBy":
                if "members" not in collections:
                    continue

                member_users = {
                    str(record.get("userId"))
                    for record in collections["members"]
                    if record.get("userId") is not None
                }
                source_values = {
                    str(record.get(field))
                    for record in records
                    if record.get(field) is not None
                }

                matches = len(source_values & member_users)
                if matches:
                    relationships.append({
                        "environment": env_name,
                        "source": f"{source_name}.{field}",
                        "target": "members.userId",
                        "matches": matches,
                        "confidence": "high",
                    })
                continue

            if not field.endswith(REFERENCE_SUFFIX):
                continue

            source_values = {
                str(record.get(field))
                for record in records
                if record.get(field) is not None
            }
            if not source_values:
                continue

            stem = field[:-2].lower()

            for target_name, target_ids in ids.items():
                singular = target_name.rstrip("s").lower()

                if stem not in {singular, target_name.lower()}:
                    continue

                matches = len(source_values & target_ids)
                if matches:
                    relationships.append({
                        "environment": env_name,
                        "source": f"{source_name}.{field}",
                        "target": f"{target_name}.id",
                        "matches": matches,
                        "confidence": "high",
                    })

    # Remove duplicates while preserving order.
    seen = set()
    unique = []
    for relationship in relationships:
        key = (
            relationship["environment"],
            relationship["source"],
            relationship["target"],
        )
        if key not in seen:
            seen.add(key)
            unique.append(relationship)

    return unique


def build_report(data):
    environments = data.get("environments", {})
    if not isinstance(environments, dict):
        raise ValueError("Expected an environment-based export with an 'environments' object.")

    report = {
        "report": {
            "name": "inCharge Dataset Discovery Report",
            "format": "environment_based",
        },
        "export": {
            "generatedAt": data.get("generatedAt"),
            "tool": data.get("tool"),
            "environment_count": len(environments),
        },
        "environments": {},
        "relationships": [],
        "observations": [],
    }

    for environment_name, environment in environments.items():
        if not isinstance(environment, dict):
            continue

        collections = collection_data(environment)

        report["environments"][environment_name] = {
            "region": environment.get("region"),
            "collections": {},
        }

        for collection_name, records in collections.items():
            report["environments"][environment_name]["collections"][collection_name] = {
                "description": COLLECTION_HINTS.get(
                    collection_name,
                    "Collection discovered in the export."
                ),
                "record_count": len(records),
                "fields": inspect_collection(records),
            }

        report["relationships"].extend(
            find_relationships(environment_name, environment)
        )

    report["observations"] = [
        f"{len(environments)} independent environment(s) detected.",
        "Collection structure and relationships were discovered from the supplied export.",
        "Relationships are reported only where identifier values were observed to match.",
        "Raw PII values are not included in this report.",
    ]

    return report


def esc(value):
    return html.escape(str(value))


def erd_svg(environment_name, relationships):
    """Render a clean, dependency-free ERD from observed relationships."""
    relevant = [
        r for r in relationships
        if r["environment"] == environment_name
        and ".id" in r["target"]
    ]

    if not relevant:
        return "<p>No observed relationships found for this environment.</p>"

    collections = set()
    foreign_keys = defaultdict(list)

    for r in relevant:
        source_name, source_field = r["source"].split(".", 1)
        target_name = r["target"].split(".", 1)[0]
        collections.update((source_name, target_name))
        foreign_keys[source_name].append(source_field)

    # Logical layout follows the actual inCharge data model.
    preferred = [
        "teams", "members", "profiles", "profileDefinitions",
        "dailySummaries", "dailySummaryVersions",
        "teamSchedules", "shifts",
    ]
    ordered = [x for x in preferred if x in collections]
    ordered += sorted(collections - set(ordered))

    width, height = 250, 105
    positions = {
        "teams": (40, 40),
        "members": (340, 40),
        "profiles": (640, 40),
        "profileDefinitions": (940, 40),
        "dailySummaries": (40, 260),
        "dailySummaryVersions": (340, 260),
        "teamSchedules": (640, 260),
        "shifts": (940, 260),
    }

    # Fallback placement for unexpected collections.
    for i, name in enumerate(x for x in ordered if x not in positions):
        positions[name] = (40 + (i % 4) * 300, 480 + (i // 4) * 190)

    max_x = max(x for x, _ in positions.values()) + width + 40
    max_y = max(y for _, y in positions.values()) + height + 60

    lines = [
        f'<svg viewBox="0 0 {max_x} {max_y}" width="100%" '
        f'xmlns="http://www.w3.org/2000/svg">',
        '<defs><marker id="arrow" markerWidth="8" markerHeight="8" '
        'refX="7" refY="3" orient="auto">'
        '<path d="M0,0 L0,6 L7,3 z" fill="#777"/></marker></defs>',
        '<style>'
        '.box{fill:#fff;stroke:#555;stroke-width:1.5}'
        '.title{font:bold 15px Arial}.field{font:12px Arial}'
        '.edge{stroke:#777;stroke-width:1.5;fill:none}'
        '.label{font:11px Arial;fill:#333}'
        '.labelbg{fill:#fff;opacity:.95}'
        '</style>',
    ]

    def anchor(source, target):
        sx, sy = positions[source]
        tx, ty = positions[target]
        scx, scy = sx + width / 2, sy + height / 2
        tcx, tcy = tx + width / 2, ty + height / 2

        if abs(tcx - scx) >= abs(tcy - scy):
            if tcx >= scx:
                return (sx + width, scy), (tx, tcy)
            return (sx, scy), (tx + width, tcy)
        if tcy >= scy:
            return (scx, sy + height), (tcx, ty)
        return (scx, sy), (tcx, ty + height)

    # Draw edges with simple orthogonal routing to avoid text/line collisions.
    for r in relevant:
        source, source_field = r["source"].split(".", 1)
        target = r["target"].split(".", 1)[0]
        if source not in positions or target not in positions:
            continue

        (x1, y1), (x2, y2) = anchor(source, target)
        if abs(y2 - y1) < 2 or abs(x2 - x1) < 2:
            path = f"M{x1},{y1} L{x2},{y2}"
            lx, ly = (x1 + x2) / 2, (y1 + y2) / 2 - 8
        else:
            mid_x = (x1 + x2) / 2
            path = f"M{x1},{y1} L{mid_x},{y1} L{mid_x},{y2} L{x2},{y2}"
            lx, ly = mid_x + 5, (y1 + y2) / 2 - 4

        lines.append(f'<path class="edge" d="{path}" marker-end="url(#arrow)"/>')
        label = esc(source_field)
        label_width = max(34, len(source_field) * 6.2 + 10)
        lines.append(
            f'<rect class="labelbg" x="{lx - label_width/2}" y="{ly - 11}" '
            f'width="{label_width}" height="16" rx="3"/>'
        )
        lines.append(
            f'<text class="label" x="{lx}" y="{ly}" text-anchor="middle">'
            f'{label}</text>'
        )

    # Draw entity boxes.
    for name in ordered:
        x, y = positions[name]
        fields = ["PK id"]
        fields.extend(f"FK {f}" for f in sorted(set(foreign_keys.get(name, []))))
        box_height = max(height, 42 + len(fields) * 17)
        positions[name] = (x, y)
        lines.append(
            f'<rect class="box" x="{x}" y="{y}" width="{width}" '
            f'height="{box_height}" rx="9"/>'
        )
        lines.append(
            f'<text class="title" x="{x + 14}" y="{y + 24}">{esc(name)}</text>'
        )
        for i, field in enumerate(fields):
            lines.append(
                f'<text class="field" x="{x + 14}" y="{y + 48 + i * 17}">'
                f'{esc(field)}</text>'
            )

    lines.append("</svg>")
    return "".join(lines)


def render_html(report):
    parts = [
        """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>inCharge Dataset Discovery Report</title>
<style>
body{font-family:Arial,sans-serif;margin:40px;color:#222;line-height:1.45}
h1{margin-bottom:6px}
h2{margin-top:38px;border-bottom:2px solid #ddd;padding-bottom:6px}
h3{margin-top:28px}
.meta{color:#666}
.cards{display:flex;gap:14px;flex-wrap:wrap;margin:22px 0}
.card{border:1px solid #ddd;border-radius:8px;padding:14px 18px;min-width:150px}
table{border-collapse:collapse;width:100%;margin:12px 0 30px}
th,td{border:1px solid #ddd;padding:7px;text-align:left;vertical-align:top}
th{background:#f3f3f3}
.tree{font-family:monospace;background:#f7f7f7;padding:18px;border-radius:8px;white-space:pre}
.erd{border:1px solid #ddd;border-radius:8px;padding:20px;overflow:auto}
.small{font-size:.9em;color:#666}
</style>
</head>
<body>
<h1>inCharge Dataset Discovery Report</h1>
<p class="meta">Generated from the supplied export. This report describes the data structure and observed relationships. It does not contain raw PII values.</p>
"""
    ]

    export = report["export"]
    parts.append(
        f"""<div class="cards">
<div class="card"><b>Format</b><br>{esc(report["report"]["format"])}</div>
<div class="card"><b>Environments</b><br>{export["environment_count"]}</div>
<div class="card"><b>Generated</b><br>{esc(export.get("generatedAt") or "Not supplied")}</div>
</div>"""
    )

    parts.append("<h2>Data Model Overview</h2><div class='tree'>")
    environment_names = list(report["environments"])

    for env_index, environment_name in enumerate(environment_names):
        parts.append(f"{esc(environment_name)}\n")
        collections = report["environments"][environment_name]["collections"]

        for index, (name, details) in enumerate(collections.items()):
            branch = "└── " if index == len(collections) - 1 else "├── "
            parts.append(
                f"    {branch}{esc(name)} "
                f"[{details['record_count']} records]\n"
            )

        if env_index < len(environment_names) - 1:
            parts.append("\n")

    parts.append("</div>")

    for environment_name, environment in report["environments"].items():
        parts.append(f"<h2>{esc(environment_name)}</h2>")

        parts.append(
            "<p><b>Region:</b> "
            f"{esc(environment.get('region') or 'Not supplied')}</p>"
        )

        parts.append(
            "<h3>Collections</h3>"
            "<table><tr><th>Collection</th><th>Records</th>"
            "<th>Description</th></tr>"
        )

        for name, details in environment["collections"].items():
            parts.append(
                f"<tr><td>{esc(name)}</td>"
                f"<td>{details['record_count']}</td>"
                f"<td>{esc(details['description'])}</td></tr>"
            )

        parts.append("</table>")

        parts.append("<h3>Fields</h3>")

        for name, details in environment["collections"].items():
            parts.append(
                f"<h4>{esc(name)} "
                f"<span class='small'>({details['record_count']} records)</span></h4>"
            )
            parts.append(
                "<table><tr><th>Field</th><th>Type</th>"
                "<th>Classification</th><th>Nulls</th></tr>"
            )

            for field in details["fields"]:
                parts.append(
                    f"<tr><td>{esc(field['field'])}</td>"
                    f"<td>{esc(field['type'])}</td>"
                    f"<td>{esc(field['classification'] or '')}</td>"
                    f"<td>{field['null_count']}</td></tr>"
                )

            parts.append("</table>")

        parts.append("<h3>ERD</h3><div class='erd'>")
        parts.append(
            erd_svg(environment_name, report["relationships"])
        )
        parts.append("</div>")

        relationships = [
            r for r in report["relationships"]
            if r["environment"] == environment_name
        ]

        parts.append("<h3>Observed Relationships</h3>")

        if relationships:
            parts.append(
                "<table><tr><th>Source</th><th>Target</th>"
                "<th>Observed Matches</th><th>Confidence</th></tr>"
            )

            for relationship in relationships:
                parts.append(
                    f"<tr><td>{esc(relationship['source'])}</td>"
                    f"<td>{esc(relationship['target'])}</td>"
                    f"<td>{relationship['matches']}</td>"
                    f"<td>{esc(relationship['confidence'])}</td></tr>"
                )

            parts.append("</table>")
        else:
            parts.append("<p>No observed relationships found.</p>")

    parts.append("<h2>Overall Observations</h2><ul>")

    for observation in report["observations"]:
        parts.append(f"<li>{esc(observation)}</li>")

    parts.append("</ul></body></html>")

    return "".join(parts)


def main():
    parser = argparse.ArgumentParser(
        description="Inspect an inCharge JSON export and create dataset discovery reports."
    )
    parser.add_argument("input", help="Path to the JSON export.")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory for generated reports. Defaults to the input file directory.",
    )

    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: input file not found: {input_path}", file=__import__("sys").stderr)
        return 1

    output_dir = Path(args.output_dir) if args.output_dir else input_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        with input_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)

        report = build_report(data)

        json_path = output_dir / "dataset_report.json"
        html_path = output_dir / "dataset_report.html"

        json_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        html_path.write_text(
            render_html(report),
            encoding="utf-8",
        )

    except Exception as exc:
        print(f"Error: {exc}", file=__import__("sys").stderr)
        return 1

    print("Schema inspection complete.")
    print(f"Report created: {html_path}")
    print(f"Machine-readable report created: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())