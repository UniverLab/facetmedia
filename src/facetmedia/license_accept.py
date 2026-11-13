"""``facet license accept``: one model by id, or a wizard for a person.

Accepting a licence is a human act, so at a terminal with no model id the verb
lists what is pending, lets the person pick one by number and asks them to type
that model's id back. An agent names the model and passes ``--yes`` instead.
Nothing is recorded unless the typed text is exactly the model id.
"""

from __future__ import annotations

import argparse
import sys
from contextlib import AbstractContextManager
from typing import Any

from facetmedia import contract, env, licenses, lock
from facetmedia.contract import FacetError
from facetmedia.home import Home
from facetmedia.registry import load_active_registry

LIST_PROMPT = "[number / q] "
RETRY = "enter a number from the list, or q"
NO_PENDING = "no model needs acceptance"
MOOT_LINE = "nothing to accept"
DIRECT_FORM = "facet license accept <model-id>"


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
    """List, pick, confirm; return to the list after every answer."""
    while True:
        pending = _pending()
        if not pending:
            print(NO_PENDING)
            return 0
        _show(pending)
        index = _ask(len(pending))
        if index is None:
            return 0
        _confirm(args, str(pending[index]["id"]))


def _show(pending: list[dict[str, Any]]) -> None:
    """One numbered line per pending model, with its package's install state."""
    registry = load_active_registry()
    home = Home.peek_env()
    for index, row in enumerate(pending, start=1):
        package = registry.packages[str(row["package"])]
        installed = "installed" if env.is_installed(package, home) else "not installed"
        commercial = "allowed" if row["commercial_use"] else "not allowed"
        print(
            f"{index}) {row['id']}  {row['package']} ({installed})  "
            f"{row['spdx']}  commercial use {commercial}"
        )


def _ask(count: int) -> int | None:
    """The picked index, or ``None`` to quit; anything else asks again."""
    while True:
        try:
            answer = input(LIST_PROMPT)
        except EOFError:
            return None
        word = answer.strip().lower()
        if word == "q":
            return None
        if word.isdigit() and 1 <= int(word) <= count:
            return int(word) - 1
        print(RETRY)


def _confirm(args: argparse.Namespace, model_id: str) -> None:
    """Ask for the exact id, then say what happened and go back to the list."""
    try:
        with _held(args):
            outcome = licenses.accept(model_id, assume_yes=False)
    except FacetError as exc:
        print(exc.message)
        return
    if outcome.state == licenses.ACCEPTED:
        print(f"{model_id} accepted")
    elif outcome.state == licenses.CANCELLED:
        print(licenses.CANCELLED_LINE)
