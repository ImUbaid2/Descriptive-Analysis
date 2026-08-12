"""Deterministic local anonymisation for care-management JSON exports.

The original export is never written to disk.  This module returns an in-memory,
relationship-preserving copy with names, emails, phone numbers and recognised
place names replaced by consistent aliases.
"""

from __future__ import annotations

import copy
import re
from collections import defaultdict
from typing import Any

NAME_KEYS = {"fullname", "full_name", "firstname", "first_name", "surname", "lastname", "last_name", "displayname", "display_name"}
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)")
PLACE_PATTERNS = (
    re.compile(r"\b(?:lives?|live|based|resides?)\s+(?:at|in)\s+([A-Z][a-z]+)"),
    re.compile(r"\bin\s+([A-Z][A-Za-z]+)\b"),
    re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}\s+(?:Surgery|Hospital|Clinic|Pharmacy|Care Home|Medical Centre))\b"),
    re.compile(r"\b(?:at|to|from|near)\s+([A-Z][A-Za-z]+)\b"),
)
PLACE_STOP_WORDS = {"GP", "Radio", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}
TITLED_PERSON_RE = re.compile(r"\b(?:Dr|Mr|Mrs|Ms|Mx)\.?\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b")


def _collections(data: dict[str, Any], *names: str) -> list[dict[str, Any]]:
    wanted = {name.lower() for name in names}
    for key, value in data.items():
        if key.lower() in wanted and isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _full_name(member: dict[str, Any]) -> str:
    full = member.get("fullName") or member.get("full_name") or member.get("displayName")
    if full:
        return str(full)
    return " ".join(str(part) for part in (member.get("firstname") or member.get("firstName") or "", member.get("surname") or member.get("lastName") or "") if part)


def _place_aliases(data: dict[str, Any]) -> dict[str, str]:
    found: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str):
            for pattern in PLACE_PATTERNS:
                for match in pattern.finditer(value):
                    candidate = match.group(1).strip()
                    if candidate not in PLACE_STOP_WORDS and candidate not in found:
                        found.append(candidate)

    visit(data)
    return {place: f"Location {index}" for index, place in enumerate(found, start=1)}


def _titled_person_aliases(data: dict[str, Any]) -> dict[str, str]:
    """Anonymise clinician/contact names written in free-text care notes."""
    found: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str):
            for match in TITLED_PERSON_RE.finditer(value):
                full_name = match.group(0)
                if full_name not in found:
                    found.append(full_name)

    visit(data)
    return {name: f"Clinician or Contact {index}" for index, name in enumerate(found, start=1)}


def anonymise_export(data: dict[str, Any]) -> dict[str, Any]:
    """Return a deep-copied export with identifying names and locations replaced.

    IDs are retained so schedules, summaries, profiles and members remain linked.
    Aliases are stable within one upload, but intentionally do not persist between
    uploads.  This is a rule-based safeguard, not a substitute for a formal DLP
    review of a new data source.
    """
    anonymised = copy.deepcopy(data)
    members = _collections(anonymised, "Member", "members")
    name_aliases: dict[str, str] = {}
    counters: defaultdict[str, int] = defaultdict(int)

    for member in members:
        role = str(member.get("role", "")).replace("_", "-").lower()
        prefix = "Central Person" if role == "central-person" or member.get("isSupportedPerson") else "Member"
        counters[prefix] += 1
        alias = f"{prefix} {counters[prefix]}"
        original = _full_name(member)
        if original:
            name_aliases[original] = alias
        for key in ("firstname", "firstName", "first_name", "surname", "lastName", "last_name", "fullName", "full_name", "displayName", "display_name"):
            if member.get(key):
                name_aliases[str(member[key])] = alias
                member[key] = alias
        if member.get("email"):
            member["email"] = f"member{sum(counters.values())}@example.invalid"

    # Team/group names can identify an organisation or cohort, so replace them too.
    for collection_name, prefix in (("Team", "Team"), ("TeamGroup", "Team Group")):
        for index, row in enumerate(_collections(anonymised, collection_name), start=1):
            if row.get("name"):
                row["name"] = f"{prefix} {index}"

    replacements = {**name_aliases, **_place_aliases(anonymised), **_titled_person_aliases(anonymised)}
    ordered_replacements = sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True)

    def clean_text(value: str) -> str:
        result = value
        for original, alias in ordered_replacements:
            result = re.sub(re.escape(original), alias, result, flags=re.IGNORECASE)
        result = EMAIL_RE.sub("[redacted email]", result)
        return PHONE_RE.sub("[redacted phone]", result)

    def visit(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: visit(child) for key, child in value.items()}
        if isinstance(value, list):
            return [visit(child) for child in value]
        return clean_text(value) if isinstance(value, str) else value

    return visit(anonymised)
