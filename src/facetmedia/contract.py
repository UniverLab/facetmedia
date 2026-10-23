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
"""

from __future__ import annotations

import json
import sys

from facetmedia import __version__

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


def ok(data: dict[str, object]) -> dict[str, object]:
    return {
        "contract": CONTRACT_VERSION,
        "data": data,
        "facet": __version__,
        "ok": True,
    }


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
        print(message, file=sys.stderr)
    hint = error.get("hint")
    if isinstance(hint, str) and hint:
        print(hint, file=sys.stderr)
