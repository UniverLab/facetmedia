"""Command-line interface for facet."""

from __future__ import annotations

import argparse
import sys
import traceback
from collections.abc import Callable
from typing import Any, NoReturn, TypeVar

from facetmedia import __version__, contract
from facetmedia.contract import FacetError

Handler = Callable[[argparse.Namespace], int]

_P = TypeVar("_P", bound=argparse.ArgumentParser)

_VERBS: tuple[tuple[str, str], ...] = (
    ("update", "refresh the registry index"),
    ("pull", "install a tool into its own pinned environment"),
    ("list", "list installed and available tools"),
    ("describe", "show a tool's manifest"),
    ("check", "verify an installed tool"),
    ("run", "run a tool through the --json contract"),
    ("preview", "render a tool's output for a person"),
    ("rm", "remove an installed tool"),
    ("gc", "remove unused environments and caches"),
)

_LICENSE_VERBS: tuple[tuple[str, str], ...] = (
    ("accept", "accept the license of a tool"),
    ("list", "list the licenses of installed tools"),
)

_REGISTRY_VERBS: tuple[tuple[str, str], ...] = (("validate", "validate a registry checkout"),)


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


def _add_json(parser: argparse.ArgumentParser) -> None:
    # SUPPRESS keeps a subcommand's default from overwriting a --json given before it.
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print the result as JSON",
    )


def _add_verb(
    subparsers: argparse._SubParsersAction[_P],
    name: str,
    help_text: str,
    handler: Handler = _not_implemented,
) -> None:
    parser = subparsers.add_parser(name, help=help_text, description=help_text)
    _add_json(parser)
    parser.set_defaults(handler=handler)


def _add_group(
    subparsers: argparse._SubParsersAction[_P],
    name: str,
    help_text: str,
    verbs: tuple[tuple[str, str], ...],
) -> None:
    group = subparsers.add_parser(name, help=help_text, description=help_text)
    _add_json(group)
    nested = group.add_subparsers(dest=f"{name}_command", metavar="VERB", required=True)
    for verb, verb_help in verbs:
        _add_verb(nested, verb, verb_help)


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
        _add_verb(subparsers, verb, verb_help)
    _add_group(subparsers, "license", "manage tool licenses", _LICENSE_VERBS)
    _add_group(subparsers, "registry", "inspect the package registry", _REGISTRY_VERBS)
    return parser


def main(argv: list[str] | None = None) -> int:
    as_json = "--json" in (argv if argv is not None else sys.argv)
    try:
        args = build_parser().parse_args(argv)
        as_json = bool(getattr(args, "json", as_json))
        handler: Handler = args.handler
        return handler(args)
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
