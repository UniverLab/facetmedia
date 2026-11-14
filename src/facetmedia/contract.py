"""Versioned JSON contract for every facet verb.

With ``--json``, stdout carries exactly one JSON document, an envelope with
the contract version; progress and logs go to stderr only.

Error codes and exit codes (exhaustive for v0.1):

| code                 | exit | when                                                        |
|----------------------|------|-------------------------------------------------------------|
| (success)            | 0    |                                                             |
| ``internal``         | 1    | an unexpected exception (traceback goes to the log, not stdout) |
| ``invalid_input``    | 2    | bad arguments, a failing capability schema, unknown capability |
| ``verification_failed`` | 3 | the asset was made but its check did not pass               |
| ``license_not_accepted`` | 4 | a model needs a person's acceptance that is not recorded    |
| ``machine_cannot``   | 5    | the machine does not meet the package requirements          |
| ``lock_busy``        | 6    | lock held and ``--no-wait`` given, or the wait timed out    |
| ``not_installed``    | 7    | package not installed and ``--no-pull`` given               |
| ``download_failed``  | 8    | a download failed or its sha256 did not match               |
| ``runner_failed``    | 9    | the package runner crashed or broke the runner protocol     |
| ``license_needs_person`` | 10 | ``license accept`` run without an interactive terminal     |
| ``confirmation_required`` | 11 | a multi-package pull needs a person's ``y``, none can be asked |
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterable, Mapping
from contextvars import ContextVar
from typing import Final

from rich.markup import escape

from facetmedia import __version__, theme

logger: Final[logging.Logger] = logging.getLogger("facetmedia")

CONTRACT_VERSION: int = 1

EXIT_CODES: dict[str, int] = {
    "internal": 1,
    "invalid_input": 2,
    "verification_failed": 3,
    "license_not_accepted": 4,
    "machine_cannot": 5,
    "lock_busy": 6,
    "not_installed": 7,
    "download_failed": 8,
    "runner_failed": 9,
    "license_needs_person": 10,
    "confirmation_required": 11,
}


class FacetError(Exception):
    """A facet failure with a stable code, message, hint, and details."""

    def __init__(
        self,
        code: str,
        message: str,
        hint: str | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code: str = code
        self.message: str = message
        self.hint: str | None = hint
        self.details: dict[str, object] = details if details is not None else {}

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.code]


_NOTICES: ContextVar[tuple[dict[str, object], ...]] = ContextVar("facet_notices", default=())


def set_notices(notices: Iterable[Mapping[str, object]]) -> None:
    """The notices ``ok`` adds to every envelope of this invocation."""
    _NOTICES.set(tuple(dict(notice) for notice in notices))


def ok(data: dict[str, object]) -> dict[str, object]:
    envelope: dict[str, object] = {
        "contract": CONTRACT_VERSION,
        "data": data,
        "facet": __version__,
        "ok": True,
    }
    notices = _NOTICES.get()
    if notices:
        envelope["notices"] = list(notices)
    return envelope


def failure(err: FacetError) -> dict[str, object]:
    return {
        "contract": CONTRACT_VERSION,
        "error": {
            "code": err.code,
            "details": err.details,
            "hint": err.hint,
            "message": err.message,
        },
        "facet": __version__,
        "ok": False,
    }


def emit(envelope: dict[str, object], as_json: bool) -> None:
    if as_json:
        sys.stdout.write(json.dumps(envelope, ensure_ascii=False, sort_keys=True) + "\n")
        return
    if envelope.get("ok") is True:
        return
    error = envelope.get("error")
    if not isinstance(error, dict):
        return
    message = error.get("message")
    if isinstance(message, str) and message:
        logger.info("%s", message)
        _stderr_line(message, f"[error]✗ {escape(message)}[/]")
    hint = error.get("hint")
    if isinstance(hint, str) and hint:
        logger.info("%s", hint)
        _stderr_line(hint, f"[muted]{escape(hint)}[/]")


def _stderr_line(text: str, markup: str) -> None:
    """One error line on stderr: marked at a terminal, byte-identical in a pipe."""
    if theme.styled(sys.stderr):
        theme.console(sys.stderr).print(markup, soft_wrap=True)
        return
    print(text, file=sys.stderr)
