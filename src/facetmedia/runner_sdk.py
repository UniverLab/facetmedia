"""The runner SDK: the package side of facet's runner protocol (protocol 1).

``facet pull`` copies this module into every package environment as
``facet_runner_sdk``; a package runner is then only tool code.  The runner
declares one function per verb and calls :meth:`Runner.main`; facet calls it
as ``python -I <entry> <verb> --input <request.json> --result <result.json>``
(from the manifest's ``transport.invocation_template``) and reads the result
file the SDK writes.

Stdlib only, Python >= 3.11, no third-party imports: a package's dependencies
must never be needed to speak the protocol.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final, NoReturn

__all__ = ["Request", "Runner", "SecretNotSet", "renew_secret", "secret"]

PROTOCOL_VERSION: Final[int] = 1
#: The environment prefix ``facet`` exports a declared secret under.
SECRET_PREFIX: Final[str] = "FACET_SECRET_"
#: The default depth for a request that carries none (older callers).
DEFAULT_DEPTH: Final[str] = "quick"
#: What the SDK answers when a package never registered its ``doctor`` verb.
NO_DOCTOR: Final[dict[str, Any]] = {
    "ok": False,
    "detail": "this package has no doctor",
    "fix": "report it to the package's maintainer",
}

#: The renewals file for the verb being handled, set by :meth:`Runner.run`.
_current_renewals_file: str = ""
#: The package name for the verb being handled, set by :meth:`Runner.run`.
_current_package: str = ""


class SecretNotSet(KeyError):
    """A declared secret with no stored value; names the command that sets it."""


class Request:
    """The request file facet wrote for this verb.

    ``inputs`` is the piece's inputs after schema validation (defaults
    filled, paths resolved absolute); ``out_dir`` is the temporary directory
    the runner writes its assets into; ``assets`` names the produced files
    and is only filled for ``verify`` and ``preview``; ``depth`` is the
    ``doctor`` verb's requested depth, ``"quick"`` (imports, files, versions;
    about 10 s, writes nothing) or ``"full"`` (may run the tool once on a
    throwaway output; up to 30 s).

    An outward capability's request also carries ``sources`` — the assets the
    post carries, each with its record ``id`` and whether a model
    ``generated`` it — and, for a read-back, ``post``: what the receipt
    says the network holds.  ``renewals_file`` is where the runner records a
    secret it refreshed so facet can keep the new value (see
    :func:`renew_secret`).
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
        self.verb: str = str(data.get("verb", ""))
        self.capability: str = str(data.get("capability", ""))
        self.protocol: int = int(data.get("protocol", 0))
        self.depth: str = str(data.get("depth", DEFAULT_DEPTH))
        self.renewals_file: str = str(data.get("renewals_file", ""))
        self.package: str = str(data.get("package", ""))
        self.inputs: dict[str, Any] = _mapping(data.get("inputs"))
        self.out_dir: Path = Path(str(data.get("out_dir", "")))
        self.piece_dir: Path = Path(str(data.get("piece_dir", "")))
        self.assets: list[Path] = [Path(str(item)) for item in _list(data.get("assets"))]
        self.sources: list[dict[str, Any]] = _sources(data.get("sources"))
        self.post: dict[str, Any] = _mapping(data.get("post"))
        self._models: dict[str, Path] = {
            str(key): Path(str(value)) for key, value in _mapping(data.get("models")).items()
        }

    @classmethod
    def from_file(cls, path: Path) -> Request:
        loaded: Any = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"{path}: the request is not a JSON object")
        return cls(loaded)

    def model_path(self, model_id: str) -> Path:
        """The absolute path of ``model_id`` under ``<home>/models/direct``."""
        try:
            return self._models[model_id]
        except KeyError:
            known = ", ".join(sorted(self._models)) or "none"
            raise KeyError(f"unknown model: {model_id} (this piece has: {known})") from None


class Runner:
    """Register one function per verb; :meth:`main` runs the one asked for."""

    def __init__(self) -> None:
        self._verbs: dict[str, Callable[[Request], dict[str, Any]]] = {}

    def verb(
        self, name: str
    ) -> Callable[[Callable[[Request], dict[str, Any]]], Callable[[Request], dict[str, Any]]]:
        """Decorator registering ``function`` as the handler of verb ``name``."""

        def register(
            function: Callable[[Request], dict[str, Any]],
        ) -> Callable[[Request], dict[str, Any]]:
            self._verbs[name] = function
            return function

        return register

    def main(self) -> NoReturn:
        """Run the verb from ``sys.argv``; never returns, always exits."""
        raise SystemExit(self.run(sys.argv[1:]))

    def run(self, argv: list[str]) -> int:
        """The ``main`` body as an exit code, for tests and embedders."""
        try:
            verb, input_path, result_path = _parse_argv(argv)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
        handler = self._verbs.get(verb)
        if handler is None:
            if verb == "doctor":
                # A package without a doctor still answers: the SDK writes the
                # no-doctor verdict and exits 0; facet reports it as a check.
                return _write(result_path, NO_DOCTOR)
            known = ", ".join(sorted(self._verbs)) or "none"
            print(f"unknown verb: {verb} (this runner has: {known})", file=sys.stderr)
            return 1
        try:
            request = Request.from_file(input_path)
            globals()["_current_renewals_file"] = request.renewals_file
            globals()["_current_package"] = request.package
            try:
                result = handler(request)
            finally:
                globals()["_current_renewals_file"] = ""
                globals()["_current_package"] = ""
        except (ValueError, SecretNotSet) as exc:
            print(f"error: {exc.args[0] if exc.args else exc}", file=sys.stderr)
            return 1
        except Exception:
            traceback.print_exc()
            return 1
        return _write(result_path, result)


def _write(result_path: Path, result: Any) -> int:
    try:
        result_path.write_text(
            json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError:
        traceback.print_exc()
        return 1
    return 0


def _parse_argv(argv: list[str]) -> tuple[str, Path, Path]:
    """``(verb, input, result)`` from ``VERB --input PATH --result PATH``."""
    if not argv:
        raise ValueError("usage: VERB --input REQUEST --result RESULT")
    verb, *rest = argv
    values: dict[str, Path] = {}
    position = 0
    while position < len(rest):
        flag = rest[position]
        if flag not in {"--input", "--result"} or position + 1 >= len(rest):
            raise ValueError(f"usage: VERB --input REQUEST --result RESULT (got {flag!r})")
        values[flag] = Path(rest[position + 1])
        position += 2
    missing = [flag for flag in ("--input", "--result") if flag not in values]
    if missing:
        raise ValueError(f"missing {', '.join(missing)}")
    return verb, values["--input"], values["--result"]


def secret(name: str) -> str:
    """The value of the declared secret ``name``, as facet exported it.

    ``name`` is the manifest spelling (``app-password``); the variable is
    ``FACET_SECRET_APP_PASSWORD``.  A secret the manifest declares but nobody
    stored is absent, and so is one the manifest never declared: both raise
    ``SecretNotSet`` (a ``KeyError``) naming what to run, so a runner never
    sends an empty credential.
    """
    variable = f"{SECRET_PREFIX}{name.upper().replace('-', '_')}"
    try:
        return os.environ[variable]
    except KeyError:
        if _current_package:
            message = f"secret {name} is not set; run facet secret set {_current_package} {name}"
        else:
            message = f"secret {name} is not set; run facet secret set or facet auth"
        raise SecretNotSet(message) from None


def renew_secret(name: str, value: str) -> None:
    """Record a refreshed secret so facet keeps the new value after this verb.

    Appends the renewal at once and flushes, so the value survives a crash or
    a raise later in the same verb.  ``name`` is the manifest spelling, as in
    :func:`secret`.  A name or value that is empty, or a value that contains a
    newline, is refused: a renewal facet cannot store safely is never written.
    """
    if not name or not value or "\n" in value or "\n" in name:
        raise KeyError(f"secret {name} cannot be renewed; run facet secret set or facet auth")
    path = _current_renewals_file
    if not path:
        raise KeyError(f"secret {name} cannot be renewed; run facet secret set or facet auth")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")
        handle.flush()


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _sources(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return []
