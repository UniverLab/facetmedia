"""Command-line interface for facet."""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn, TypeVar

from facetmedia import (
    __version__,
    check,
    contract,
    env,
    introspect,
    licenses,
    lock,
    registry,
    run,
    update,
)
from facetmedia.contract import FacetError
from facetmedia.home import Home, log_file, new_run_id, require_posix
from facetmedia.registry import load_active_registry, validate_registry

Handler = Callable[[argparse.Namespace], int]

_P = TypeVar("_P", bound=argparse.ArgumentParser)

_VERBS: tuple[tuple[str, str], ...] = (
    ("update", "download the registry at a pinned commit"),
    ("pull", "install a tool into its own pinned environment"),
    ("list", "list installed and available tools"),
    ("describe", "show a tool's manifest"),
    ("check", "report machine readiness or re-verify an asset"),
    ("run", "run a tool through the --json contract"),
    ("preview", "render a tool's output for a person"),
    ("rm", "remove an installed tool"),
    ("gc", "remove unused environments and caches"),
)

_LICENSE_VERBS: tuple[tuple[str, str], ...] = (
    ("accept", "accept the license of a tool"),
    ("list", "list the licenses of installed tools"),
)

# Verbs that take the facet lock get the waiting flags; light verbs (list,
# describe, preview, license list) take no lock and reject them. `check`
# without an asset takes no lock (a machine-only report); with an asset it
# holds the lock itself, so it also rejects the flags.
_LOCK_VERBS: frozenset[str] = frozenset({"gc", "pull", "rm", "run", "update"})


class FacetParser(argparse.ArgumentParser):
    _facet_argv: list[str] | None = None

    def parse_known_args(self, *args: Any, **kwargs: Any) -> Any:
        raw = args[0] if args else kwargs.get("args")
        if raw is None:
            self._facet_argv = sys.argv[1:]
        elif isinstance(raw, str):
            self._facet_argv = [raw]
        else:
            try:
                self._facet_argv = list(raw)
            except TypeError:
                self._facet_argv = sys.argv[1:]
        return super().parse_known_args(*args, **kwargs)

    def error(self, message: str) -> NoReturn:
        argv = self._facet_argv if self._facet_argv is not None else sys.argv[1:]
        if "--json" in argv:
            contract.emit(contract.failure(FacetError("invalid_input", message)), True)
        super().error(message)


def _not_implemented(_args: argparse.Namespace) -> int:
    raise FacetError("internal", "not implemented yet")


def _handle_license_accept(args: argparse.Namespace) -> int:
    with lock.hold("exclusive", "facet license accept", *_lock_params(args)):
        record = licenses.accept(args.model, assume_yes=bool(args.yes))
    if record is None:
        contract.emit(contract.ok({"model": args.model, "accepted": False}), args.json)
        if not args.json:
            print("nothing to accept")
        return 0
    contract.emit(contract.ok(record), args.json)
    if not args.json:
        print(f"{args.model} accepted")
    return 0


def _handle_license_list(args: argparse.Namespace) -> int:
    rows = licenses.list_status()
    contract.emit(contract.ok({"models": rows}), args.json)
    if not args.json:
        for row in rows:
            state = "accepted" if row["accepted"] else "not accepted"
            print(f"{row['id']} {row['spdx']} {state}")
    return 0


_LICENSE_HANDLERS: dict[str, Handler] = {
    "accept": _handle_license_accept,
    "list": _handle_license_list,
}


def _handle_update(args: argparse.Namespace) -> int:
    ref = args.ref or update.default_ref()
    with lock.hold("exclusive", "facet update", *_lock_params(args)):
        data = update.update_registry(Home.from_env(), ref)
    contract.emit(contract.ok(data), args.json)
    if not args.json:
        print(f"registry {data['commit']} (ref {data['ref']})")
        for package in _packages(data):
            suffix = " changed" if package["changed"] else ""
            print(f"  {package['name']} {package['version']}{suffix}")
    return 0


def _packages(data: dict[str, object]) -> list[dict[str, object]]:
    packages = data.get("packages")
    if not isinstance(packages, list):
        return []
    return [package for package in packages if isinstance(package, dict)]


def _handle_pull(args: argparse.Namespace) -> int:
    with lock.hold("exclusive", "facet pull", *_lock_params(args)):
        active = load_active_registry()
        package = active.package(args.package)
        if package is None:
            raise FacetError(
                "invalid_input",
                f"unknown package: {args.package}",
                details={"known": sorted(active.packages)},
            )
        report = env.pull(package, Home.from_env(), verify_models=args.verify_models)
    contract.emit(contract.ok(report), args.json)
    if not args.json:
        _announce_pull(report)
    return 0


def _announce_pull(report: dict[str, object]) -> None:
    name = f"{report['package']} {report['version']}"
    if report["already_installed"] is True:
        print(f"{name} already installed at {report['env']}")
        return
    print(
        f"{name} installed at {report['env']} "
        f"({report['downloaded_bytes']} bytes downloaded in {report['seconds']} s)"
    )


def _handle_run(args: argparse.Namespace) -> int:
    """The envelope carries every record; the exit code reports verification."""
    data, exit_code = run.execute(args, args.run_id)
    contract.emit(contract.ok(data), args.json)
    if not args.json:
        _announce_run(data)
    return exit_code


def _announce_run(data: dict[str, object]) -> None:
    assets = data.get("assets")
    for entry in assets if isinstance(assets, list) else []:
        if not isinstance(entry, dict):
            continue
        verification = entry.get("verification")
        status = (
            verification.get("status", "unknown") if isinstance(verification, dict) else "unknown"
        )
        print(f"{entry.get('path')} {entry.get('name')} {status}")
    print(f"out {data.get('out_dir')}")


def _handle_check(args: argparse.Namespace) -> int:
    """Machine-only `check` takes no lock; re-verifying an asset holds it."""
    home = Home.from_env()
    if args.asset is None:
        data, code = check.run_check(args.capability, home, args.run_id)
        contract.emit(contract.ok(data), args.json)
        if not args.json:
            _announce_check(data)
        return code
    _capability, package = check.resolve(args.capability)
    mode: lock.LockMode = "exclusive" if package.requirements.exclusive else "shared"
    wait, wait_timeout = _lock_params(args)
    with lock.hold(mode, "facet check", wait, wait_timeout):
        data, code = check.reverify(args.capability, args.asset, home, args.run_id)
    contract.emit(contract.ok(data), args.json)
    if not args.json:
        _announce_reverify(data)
    return code


def _announce_check(data: dict[str, object]) -> None:
    state = "can run" if data.get("can_run") is True else "cannot run"
    suffix = ", needs pull" if data.get("needs_pull") is True else ""
    print(f"{data.get('capability')}: {state}{suffix}")
    checks = data.get("checks")
    if not isinstance(checks, dict):
        return
    for name, entry in checks.items():
        if not isinstance(entry, dict):
            continue
        mark = "ok" if entry.get("ok") is True else "FAIL"
        print(f"  {mark} {name}: {entry.get('detail')}")


def _announce_reverify(data: dict[str, object]) -> None:
    state = "passed" if data.get("passed") is True else "failed"
    print(f"{data.get('asset')} {state}")


def _handle_list(args: argparse.Namespace) -> int:
    """A light verb: the registry snapshot and the home, no lock, no network."""
    data = introspect.list_data(args.available)
    contract.emit(contract.ok(data), args.json)
    if not args.json:
        _announce_list(data)
    return 0


def _announce_list(data: dict[str, object]) -> None:
    for package in _packages(data):
        suffix = "  [installed]" if package.get("installed") is True else ""
        capabilities = package.get("capabilities")
        for capability in capabilities if isinstance(capabilities, list) else []:
            if not isinstance(capability, dict):
                continue
            cap_id = capability.get("id")
            kind = capability.get("kind")
            summary = capability.get("summary")
            print(f"{cap_id}  {kind}  {summary}{suffix}")


def _handle_describe(args: argparse.Namespace) -> int:
    data = introspect.describe_data(args.capability)
    contract.emit(contract.ok(data), args.json)
    if not args.json:
        _announce_describe(data)
    return 0


def _announce_describe(data: dict[str, object]) -> None:
    print(f"summary: {data['summary']}")
    package = data.get("package")
    if isinstance(package, dict):
        print(
            f"package: {package.get('name')} {package.get('version')} "
            f"({package.get('tool_name')}, {package.get('provider')})"
        )
    print(f"inputs: {_describe_inputs(data)}")
    print(f"installed: {'yes' if data.get('installed') is True else 'no'}")


def _describe_inputs(data: dict[str, object]) -> str:
    """One ``name (type, required|default X)`` fragment per input field."""
    inputs = data.get("inputs")
    properties = inputs.get("properties") if isinstance(inputs, dict) else None
    required = inputs.get("required") if isinstance(inputs, dict) else None
    parts: list[str] = []
    for name, field_schema in properties.items() if isinstance(properties, dict) else []:
        if not isinstance(field_schema, dict):
            continue
        kind = field_schema.get("type", "?")
        if isinstance(required, list) and name in required:
            parts.append(f"{name} ({kind}, required)")
        elif "default" in field_schema:
            parts.append(f"{name} ({kind}, default {field_schema['default']})")
        else:
            parts.append(f"{name} ({kind})")
    return ", ".join(parts)


_HANDLERS: dict[str, Handler] = {
    "check": _handle_check,
    "describe": _handle_describe,
    "list": _handle_list,
    "pull": _handle_pull,
    "run": _handle_run,
    "update": _handle_update,
}


def _handle_registry_validate(args: argparse.Namespace) -> int:
    path = Path(args.dir)
    problems = validate_registry(path)
    if not problems:
        if args.json:
            package_names = sorted(registry.load_registry(path).packages.keys())
            contract.emit(
                contract.ok({"path": str(path), "valid": True, "packages": package_names}),
                True,
            )
        return 0
    for problem in problems:
        sys.stderr.write(f"{problem['file']}: {problem['key']}: {problem['problem']}\n")
    if args.json:
        contract.emit(
            contract.failure(
                FacetError(
                    "invalid_input", "registry validation failed", details={"errors": problems}
                )
            ),
            True,
        )
    return 2


def _add_json(parser: argparse.ArgumentParser) -> None:
    # SUPPRESS keeps a subcommand's default from overwriting a --json given before it.
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print the result as JSON",
    )


def _add_lock_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--no-wait",
        dest="wait",
        action="store_false",
        help="fail at once with lock_busy instead of waiting for the facet lock",
    )
    parser.add_argument(
        "--wait-timeout",
        type=float,
        default=3600.0,
        metavar="SECONDS",
        help="seconds to wait for the facet lock (default: 3600)",
    )


def _lock_params(args: argparse.Namespace) -> tuple[bool, float]:
    """``(wait, timeout_s)`` for a verb that takes the facet lock."""
    wait = getattr(args, "wait", True)
    timeout_s = getattr(args, "wait_timeout", 3600.0)
    return bool(wait), float(timeout_s)


def _add_verb(
    subparsers: argparse._SubParsersAction[_P],
    name: str,
    help_text: str,
    handler: Handler = _not_implemented,
    lock: bool = False,
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(name, help=help_text, description=help_text)
    _add_json(parser)
    if lock:
        _add_lock_flags(parser)
    parser.set_defaults(handler=handler)
    return parser


def _add_group(
    subparsers: argparse._SubParsersAction[_P],
    name: str,
    help_text: str,
    verbs: tuple[tuple[str, str], ...],
    lock_verbs: frozenset[str] = frozenset(),
    handlers: dict[str, Handler] | None = None,
) -> dict[str, argparse.ArgumentParser]:
    group = subparsers.add_parser(name, help=help_text, description=help_text)
    _add_json(group)
    nested = group.add_subparsers(dest=f"{name}_command", metavar="VERB", required=True)
    parsers: dict[str, argparse.ArgumentParser] = {}
    for verb, verb_help in verbs:
        handler = handlers.get(verb, _not_implemented) if handlers else _not_implemented
        parsers[verb] = _add_verb(nested, verb, verb_help, handler, lock=verb in lock_verbs)
    return parsers


def _add_registry_group(subparsers: argparse._SubParsersAction[_P]) -> None:
    group = subparsers.add_parser(
        "registry", help="inspect the package registry", description="inspect the package registry"
    )
    _add_json(group)
    nested = group.add_subparsers(dest="registry_command", metavar="VERB", required=True)
    validate = nested.add_parser(
        "validate", help="validate a registry checkout", description="validate a registry checkout"
    )
    _add_json(validate)
    validate.add_argument("dir", help="registry directory to validate")
    validate.set_defaults(handler=_handle_registry_validate)


def _add_update_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ref",
        default=None,
        metavar="REF",
        help="branch, tag, or commit to pin (default: $FACET_REGISTRY_REF or main)",
    )


def _add_list_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--available",
        action="store_true",
        help="list every package in the registry snapshot, installed or not",
    )


def _add_describe_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("capability", help="registry capability id to describe")


def _add_license_flags(parsers: dict[str, argparse.ArgumentParser]) -> None:
    accept = parsers["accept"]
    accept.add_argument("model", help="registry model id to accept")
    accept.add_argument(
        "--yes",
        action="store_true",
        help="accept without asking; for agents directed by the machine's governor",
    )


def _add_pull_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("package", help="registry package to install")
    parser.add_argument(
        "--verify-models",
        action="store_true",
        help="recompute the sha256 of models that are already present",
    )


def _add_check_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("capability", help="registry capability id to check")
    parser.add_argument(
        "asset",
        nargs="?",
        default=None,
        help="asset or record to re-verify (without it, facet reports machine readiness)",
    )


def _add_run_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("capability", help="registry capability id to run")
    parser.add_argument("piece", help="piece file to run, or - for JSON on stdin")
    parser.add_argument(
        "--out",
        default=None,
        metavar="DIR",
        help="asset directory (default: the piece file's directory)",
    )
    parser.add_argument(
        "--no-verify",
        dest="verify",
        action="store_false",
        help="skip the runner's verify verb; records say skipped",
    )
    parser.add_argument(
        "--no-pull",
        dest="pull",
        action="store_false",
        help="fail with not_installed instead of pulling the package",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="kill the runner's process group after this many seconds",
    )


def build_parser() -> FacetParser:
    parser = FacetParser(
        prog="facet",
        description="Creation tools for agents: isolated, verifiable, license-aware.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    _add_json(parser)
    parser.set_defaults(json=False)
    subparsers = parser.add_subparsers(dest="command", metavar="VERB", required=True)
    for verb, verb_help in _VERBS:
        verb_parser = _add_verb(
            subparsers,
            verb,
            verb_help,
            handler=_HANDLERS.get(verb, _not_implemented),
            lock=verb in _LOCK_VERBS,
        )
        if verb == "update":
            _add_update_flags(verb_parser)
        elif verb == "list":
            _add_list_flags(verb_parser)
        elif verb == "describe":
            _add_describe_flags(verb_parser)
        elif verb == "pull":
            _add_pull_flags(verb_parser)
        elif verb == "check":
            _add_check_flags(verb_parser)
        elif verb == "run":
            _add_run_flags(verb_parser)
    license_parsers = _add_group(
        subparsers,
        "license",
        "manage tool licenses",
        _LICENSE_VERBS,
        lock_verbs=frozenset({"accept"}),
        handlers=_LICENSE_HANDLERS,
    )
    _add_license_flags(license_parsers)
    _add_registry_group(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    run_id = new_run_id()
    as_json = "--json" in (argv if argv is not None else sys.argv)
    try:
        require_posix()
        Home.from_env()
        return _run_logged(argv, run_id)
    except FacetError as exc:
        contract.emit(contract.failure(exc), as_json)
        return exc.exit_code
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc(file=sys.stderr)
        fallback = FacetError("internal", "internal error, see stderr", details={"log": "stderr"})
        contract.emit(contract.failure(fallback), as_json)
        return 1


def _run_logged(argv: list[str] | None, run_id: str) -> int:
    as_json = "--json" in (argv if argv is not None else sys.argv)
    path = log_file(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("facetmedia")
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        try:
            args = build_parser().parse_args(argv)
            args.run_id = run_id
            as_json = bool(getattr(args, "json", as_json))
            handler_fn: Handler = args.handler
            return handler_fn(args)
        except FacetError as exc:
            contract.emit(contract.failure(exc), as_json)
            return exc.exit_code
        except SystemExit:
            raise
        except Exception:
            logger.exception("unhandled exception")
            fallback = FacetError(
                "internal",
                f"internal error, see {path}",
                details={"log": str(path)},
            )
            contract.emit(contract.failure(fallback), as_json)
            return 1
    finally:
        logger.removeHandler(handler)
        handler.close()
        logger.setLevel(previous_level)
