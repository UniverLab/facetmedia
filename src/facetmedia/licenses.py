"""Recorded licence acceptance for models that carry terms.

A model with ``requires_acceptance = true`` (OmniVoice is CC-BY-NC, for
example) is used only after the person who governs the machine has accepted
its terms once, explicitly, with ``facet license accept <model-id>``. The
record lives in ``<home>/state/licenses.json`` and carries who accepted
(the system login name) and how (``interactive`` or ``flag``).
"""

from __future__ import annotations

import getpass
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from facetmedia.contract import FacetError
from facetmedia.home import Home, atomic_write_json, read_json
from facetmedia.registry import load_active_registry
from facetmedia.registry_types import Model, Package

STATE_VERSION = 1

RESPONSIBILITY_LINE = (
    "Accepting makes the person who runs facet responsible for complying with these terms."
)
NON_COMMERCIAL_LINE = "Commercial use is not allowed."
PROMPT = "Type the model id to accept: "
NEEDS_PERSON_HINT = "run it in a terminal, or pass --yes"


def find_model(model_id: str) -> tuple[Package, Model] | None:
    """The ``(package, model)`` for ``model_id``, or ``None`` when unknown."""
    registry = load_active_registry()
    for package in registry.packages.values():
        for model in package.models:
            if model.id == model_id:
                return package, model
    return None


def acceptance_ids() -> list[str]:
    """Every model id in the registry that requires acceptance, sorted."""
    registry = load_active_registry()
    found = [
        model.id
        for package in registry.packages.values()
        for model in package.models
        if model.requires_acceptance
    ]
    return sorted(found)


def warn(package: Package, model: Model) -> None:
    """Print what is being accepted to stderr, always before accepting."""
    print(f"model {model.id}", file=sys.stderr)
    print(f"package {package.name}", file=sys.stderr)
    print(f"spdx {model.spdx}", file=sys.stderr)
    print(f"commercial_use {str(model.commercial_use).lower()}", file=sys.stderr)
    print(f"terms_url {model.terms_url}", file=sys.stderr)
    if not model.commercial_use:
        print(NON_COMMERCIAL_LINE, file=sys.stderr)
    print(RESPONSIBILITY_LINE, file=sys.stderr)


def _now_z() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_accepted(home: Home) -> list[dict[str, Any]]:
    state: object = read_json(home.licenses_file, {})
    if not isinstance(state, dict):
        return []
    records = state.get("accepted")
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, dict)]


def _matching_record(model: Model, records: list[dict[str, Any]]) -> dict[str, Any] | None:
    for record in records:
        if record.get("model") == model.id and record.get("spdx") == model.spdx:
            return record
    return None


def accept(model_id: str, assume_yes: bool) -> dict[str, Any] | None:
    """Accept ``model_id``; returns the record, or ``None`` when moot.

    Raises ``invalid_input`` for an unknown id or a mistyped confirmation,
    and ``license_needs_person`` when no ``--yes`` flag is given off-terminal.
    """
    found = find_model(model_id)
    if found is None:
        raise FacetError(
            "invalid_input",
            f"unknown model: {model_id}",
            details={"known": acceptance_ids()},
        )
    package, model = found
    if not model.requires_acceptance:
        return None
    warn(package, model)
    if assume_yes:
        method = "flag"
    else:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise FacetError(
                "license_needs_person",
                "license accept needs a person at a terminal",
                hint=NEEDS_PERSON_HINT,
            )
        answer = input(PROMPT)
        if answer != model_id:
            raise FacetError(
                "invalid_input",
                "confirmation did not match the model id; nothing was recorded",
                details={"model": model_id},
            )
        method = "interactive"
    record: dict[str, Any] = {
        "model": model.id,
        "spdx": model.spdx,
        "terms_url": model.terms_url,
        "commercial_use": model.commercial_use,
        "by": getpass.getuser(),
        "at": _now_z(),
        "method": method,
    }
    home = Home.from_env()
    kept = [
        record_item for record_item in _read_accepted(home) if record_item.get("model") != model_id
    ]
    kept.append(record)
    atomic_write_json(
        home.licenses_file, {"version": STATE_VERSION, "accepted": kept}, home.tmp_dir
    )
    return record


def list_status() -> list[dict[str, Any]]:
    """Every registry model that requires acceptance, with its record."""
    registry = load_active_registry()
    home = Home.peek_env()
    records = _read_accepted(home)
    rows: list[dict[str, Any]] = []
    for package in registry.packages.values():
        for model in package.models:
            if not model.requires_acceptance:
                continue
            match = _matching_record(model, records)
            rows.append(
                {
                    "id": model.id,
                    "package": package.name,
                    "spdx": model.spdx,
                    "terms_url": model.terms_url,
                    "commercial_use": model.commercial_use,
                    "accepted": match is not None,
                    "by": match.get("by") if match is not None else None,
                    "at": match.get("at") if match is not None else None,
                    "method": match.get("method") if match is not None else None,
                }
            )
    rows.sort(key=lambda row: str(row["id"]))
    return rows


def require(models: Iterable[Model]) -> list[dict[str, Any]]:
    """The acceptance records for ``models``; raises when one is missing.

    A record matches only when its ``spdx`` equals the manifest's current
    ``spdx``: a licence change asks again.
    """
    wanted = [model for model in models if model.requires_acceptance]
    if not wanted:
        return []
    home = Home.peek_env()
    records = _read_accepted(home)
    matched: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    for model in wanted:
        match = _matching_record(model, records)
        if match is not None:
            matched.append(match)
            continue
        missing.append(
            {
                "id": model.id,
                "spdx": model.spdx,
                "terms_url": model.terms_url,
                "command": f"facet license accept {model.id}",
            }
        )
    if missing:
        raise FacetError(
            "license_not_accepted",
            "license not accepted: "
            + ", ".join(f"{entry['id']} ({entry['spdx']})" for entry in missing),
            hint="; ".join(str(entry["command"]) for entry in missing),
            details={"models": missing},
        )
    return matched
