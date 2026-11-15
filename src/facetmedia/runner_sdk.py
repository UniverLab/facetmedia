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
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final, NoReturn

__all__ = ["Request", "Runner"]

PROTOCOL_VERSION: Final[int] = 1
#: The default depth for a request that carries none (older callers).
DEFAULT_DEPTH: Final[str] = "quick"
#: What the SDK answers when a package never registered its ``doctor`` verb.
NO_DOCTOR: Final[dict[str, Any]] = {
    "ok": False,
    "detail": "this package has no doctor",
    "fix": "report it to the package's maintainer",
}


class Request:
    """The request file facet wrote for this verb.

    ``inputs`` is the piece's inputs after schema validation (defaults
    filled, paths resolved absolute); ``out_dir`` is the temporary directory
    the runner writes its assets into; ``assets`` names the produced files
    and is only filled for ``verify`` and ``preview``; ``depth`` is the
    ``doctor`` verb's requested depth, ``"quick"`` (imports, files, versions;
    about 10 s, writes nothing) or ``"full"`` (may run the tool once on a
    throwaway output; up to 30 s).
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
        self.verb: str = str(data.get("verb", ""))
        self.capability: str = str(data.get("capability", ""))
        self.protocol: int = int(data.get("protocol", 0))
        self.depth: str = str(data.get("depth", DEFAULT_DEPTH))
        self.inputs: dict[str, Any] = _mapping(data.get("inputs"))
        self.out_dir: Path = Path(str(data.get("out_dir", "")))
        self.piece_dir: Path = Path(str(data.get("piece_dir", "")))
        self.assets: list[Path] = [Path(str(item)) for item in _list(data.get("assets"))]
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
            result = handler(request)
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


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return []
