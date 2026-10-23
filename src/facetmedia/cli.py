"""Command-line interface for facet."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from facetmedia import __version__

Handler = Callable[[argparse.Namespace], int]

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


def _not_implemented(_args: argparse.Namespace) -> int:
    print("not implemented yet", file=sys.stderr)
    return 1


def _add_json(parser: argparse.ArgumentParser) -> None:
    # SUPPRESS keeps a subcommand's default from overwriting a --json given before it.
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print the result as JSON",
    )


def _add_verb(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    help_text: str,
    handler: Handler = _not_implemented,
) -> None:
    parser = subparsers.add_parser(name, help=help_text, description=help_text)
    _add_json(parser)
    parser.set_defaults(handler=handler)


def _add_group(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    help_text: str,
    verbs: tuple[tuple[str, str], ...],
) -> None:
    group = subparsers.add_parser(name, help=help_text, description=help_text)
    _add_json(group)
    nested = group.add_subparsers(dest=f"{name}_command", metavar="VERB", required=True)
    for verb, verb_help in verbs:
        _add_verb(nested, verb, verb_help)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
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
    args = build_parser().parse_args(argv)
    handler: Handler = args.handler
    return handler(args)
