"""``facet license accept``: one model by id, or a wizard for a person.

Accepting a licence is a human act, so at a terminal with no model id the
verb lists what is pending, lets the person pick one with the arrow keys
and asks them to type that model's id back. An agent names the model and
passes ``--yes`` instead. Nothing is recorded unless the typed text is
exactly the model id.
"""

from __future__ import annotations

import argparse
import sys
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any, Final

import questionary
from rich.panel import Panel

from facetmedia import banner, contract, env, licenses, lock, theme
from facetmedia.contract import FacetError
from facetmedia.home import Home
from facetmedia.registry import load_active_registry

if TYPE_CHECKING:
    from facetmedia.registry_types import Registry

NO_PENDING = "no model needs acceptance"
MOOT_LINE = "nothing to accept"
DIRECT_FORM = "facet license accept <model-id>"
_DONE: Final = object()
SELECT_MESSAGE = "Choose a model — arrows move, enter picks [default: done]:"


def type_prompt(model_id: str) -> str:
    """The typing prompt: the exact text to type, cancelling by default."""
    return f'Type "{model_id}" to accept [default: cancel]: '


def handle(args: argparse.Namespace) -> int:
    """One model from the command line, or the wizard when no id is named."""
    model_id = getattr(args, "model", None)
    if model_id is None:
        return _without_model(args)
    with _held(args):
        outcome = licenses.accept(str(model_id), assume_yes=bool(args.yes))
    return _report(outcome, str(model_id), args)


def _held(args: argparse.Namespace) -> AbstractContextManager[None]:
    """The facet lock around one acceptance, never around the wizard itself."""
    wait = bool(getattr(args, "wait", True))
    timeout_s = float(getattr(args, "wait_timeout", 3600.0))
    return lock.hold("exclusive", "facet license accept", wait, timeout_s)


def _report(outcome: licenses.Acceptance, model_id: str, args: argparse.Namespace) -> int:
    """Print what the acceptance did: recorded, cancelled, or nothing to accept."""
    as_json = bool(args.json)
    if outcome.state == licenses.CANCELLED:
        contract.emit(
            contract.ok({"model": model_id, "accepted": False, "cancelled": True}), as_json
        )
        if not as_json:
            print(licenses.CANCELLED_LINE)
        return 0
    if outcome.state == licenses.MOOT:
        contract.emit(contract.ok({"model": model_id, "accepted": False}), as_json)
        if not as_json:
            print(MOOT_LINE)
        return 0
    contract.emit(contract.ok(outcome.record or {}), as_json)
    if not as_json:
        print(f"{model_id} accepted")
    return 0


def _without_model(args: argparse.Namespace) -> int:
    """The wizard at a terminal; every other case names the model to pass."""
    if bool(args.yes) or bool(args.json) or not _is_terminal():
        raise FacetError("invalid_input", _direct_message())
    return _run(args)


def _is_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _direct_message() -> str:
    """What to run, and which ids are pending, for the id-only form."""
    pending = [str(row["id"]) for row in _pending()]
    tail = ", ".join(pending) if pending else "no model needs acceptance"
    return f"name a model to accept: {DIRECT_FORM}; pending: {tail}"


def _pending() -> list[dict[str, Any]]:
    """Every model still awaiting acceptance; re-read on each wizard pass."""
    return [row for row in licenses.list_status() if not row["accepted"]]


def _run(args: argparse.Namespace) -> int:
    """Banner, then pick and confirm; back to the list after every answer."""
    banner.print_text()
    while True:
        pending = _pending()
        if not pending:
            print(NO_PENDING)
            return 0
        model_id = _select(pending)
        if model_id is None:
            return 0
        _confirm(args, model_id)


def _select(pending: list[dict[str, Any]]) -> str | None:
    """The picked pending model id, or ``None`` on done."""
    registry = load_active_registry()
    home = Home.peek_env()
    prompts = [
        questionary.Choice(title=_model_line(row, registry, home), value=str(row["id"]))
        for row in pending
    ]
    prompts.append(questionary.Choice(title="done", value=_DONE))
    picked = questionary.select(SELECT_MESSAGE, choices=prompts).unsafe_ask()
    if picked is _DONE:
        return None
    return str(picked)


def _model_line(row: dict[str, Any], registry: Registry, home: Home) -> str:
    """One pending model: id, package, installed or not, SPDX, commercial use."""
    package = registry.packages[str(row["package"])]
    installed = "installed" if env.is_installed(package, home) else "not installed"
    commercial = "allowed" if row["commercial_use"] else "not allowed"
    return (
        f"{row['id']}  {row['package']} ({installed})  {row['spdx']}  commercial use {commercial}"
    )


def _confirm(args: argparse.Namespace, model_id: str) -> None:
    """Show the terms, ask for the exact id, and say what happened."""
    found = licenses.find_model(model_id)
    if found is None:
        return
    package, model = found
    licenses.warn(package, model)
    theme.console().print(Panel(_terms_text(model_id), title=f"Terms for {model_id}"))
    typed = questionary.text(type_prompt(model_id)).unsafe_ask()
    try:
        verdict = licenses.decide(model_id, typed)
    except FacetError as exc:
        print(exc.message)
        return
    if verdict.state == licenses.CANCELLED:
        print(licenses.CANCELLED_LINE)
        return
    with _held(args):
        licenses.record(model, "interactive")
    print(f"{model_id} accepted")


def _terms_text(model_id: str) -> str:
    """The terms the person accepts, as shown above the typing prompt."""
    found = licenses.find_model(model_id)
    if found is None:
        return ""
    _package, model = found
    lines = [
        f"model {model.id}",
        f"package {found[0].name}",
        f"spdx {model.spdx}",
        f"commercial_use {str(model.commercial_use).lower()}",
        f"terms_url {model.terms_url}",
    ]
    if not model.commercial_use:
        lines.append(licenses.NON_COMMERCIAL_LINE)
    lines.append(licenses.RESPONSIBILITY_LINE)
    return "\n".join(lines)
