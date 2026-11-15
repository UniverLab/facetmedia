"""Argument parsing for the facet CLI.

Every positional placeholder says what it takes and where its values come
from, every verb's help ends with real examples (the table lives in
``facetmedia.cli``), and the top-level help groups the everyday verbs under
three headings.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Mapping
from typing import Any, ClassVar, NoReturn, TypeVar

from rich.panel import Panel
from rich.text import Text
from rich_argparse import RawDescriptionRichHelpFormatter

from facetmedia import __version__, contract, example, preview, theme
from facetmedia.contract import FacetError
from facetmedia.registry_types import FACETS

Handler = Callable[[argparse.Namespace], int]

Examples = Mapping[tuple[str, ...], tuple[str, ...]]

_P = TypeVar("_P", bound=argparse.ArgumentParser)

_USAGE_EXIT = 2

_VERB_HELP: dict[str, str] = {
    "update": "download the registry at a pinned commit",
    "pull": "install a tool into its own pinned environment",
    "list": "list installed and available tools",
    "describe": "show one capability's contract",
    "example": "copy a capability's bundled example into a folder",
    "run": "run a tool through the --json contract",
    "check": "re-verify an asset against its record",
    "preview": "render a tool's output for a person",
    "doctor": "report facet, this machine's readiness and each package's doctor",
    "remove": "remove an installed tool",
    "clean": "remove unused environments and caches",
    "skill": "print the skill for facet or one package",
}

_GROUP_HELP: dict[str, str] = {
    "license": "manage tool licenses",
    "config": "set, get and list host application paths",
}

_VERB_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("start", ("update", "pull", "list", "describe", "example")),
    ("make and check", ("run", "check", "preview")),
    ("maintain", ("doctor", "license", "config", "remove", "clean", "skill")),
)

_GROUP_VERBS: frozenset[str] = frozenset(_GROUP_HELP)

_VERBS: tuple[str, ...] = tuple(
    verb for _title, verbs in _VERB_GROUPS for verb in verbs if verb not in _GROUP_VERBS
)

_LICENSE_VERBS: tuple[tuple[str, str], ...] = (
    ("accept", "accept the license of a tool"),
    ("list", "list the licenses of installed tools"),
)

_CONFIG_VERBS: tuple[tuple[str, str], ...] = (
    ("set", "store a host application's path"),
    ("get", "print one host application config value"),
    ("list", "list the host application config keys"),
)

# Verbs that take the facet lock get the waiting flags; light verbs (list,
# describe, preview, license list) take no lock and reject them. `check`
# holds the lock itself, so it also rejects the flags.
_LOCK_VERBS: frozenset[str] = frozenset(
    {"doctor", "clean", "pull", "remove", "run", "update", "example"}
)


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


def _styled_group_name(name: str) -> str:
    """A help section heading: sub-verbs live under ``Commands``."""
    if name == "positional arguments":
        return "Commands"
    return str.title(name)


class FacetHelpFormatter(RawDescriptionRichHelpFormatter):
    """Styled help for a person at a terminal.

    Calm colours only (see ``facetmedia.theme``): the default foreground with
    ``bold`` and ``dim``.  Used only when standard output is a styled TTY;
    everywhere else the plain ``RawDescriptionHelpFormatter`` keeps the output
    byte-identical to what it is today.
    """

    group_name_formatter: ClassVar[Callable[[str], str]] = _styled_group_name
    styles: ClassVar[dict[str, Any]] = {
        "argparse.args": "bold",
        "argparse.groups": "bold",
        "argparse.help": "default",
        "argparse.metavar": "default",
        "argparse.prog": "default",
        "argparse.default": "dim",
        "argparse.text": "default",
        "argparse.syntax": "bold",
    }
    help_markup: ClassVar[bool] = False
    text_markup: ClassVar[bool] = False
    highlights: ClassVar[list[str]] = []

    def add_text(self, text: Any) -> None:
        """The top-level verb groups as one panel per group, not plain text."""
        if isinstance(text, str) and text == _grouped_help():
            self._add_group_panels()
            return
        super().add_text(text)

    def _add_group_panels(self) -> None:
        """One titled panel per verb group, then the maintainers line."""
        for title, verbs in _VERB_GROUPS:
            body = "\n".join(
                f"{verb:<9} {_VERB_HELP.get(verb) or _GROUP_HELP[verb]}" for verb in verbs
            )
            self.add_renderable(Panel(Text(body, style="argparse.text"), title=title))
        super().add_text("maintainers: facet registry validate <registry-dir>")


def _formatter_class() -> type[argparse.HelpFormatter]:
    """Rich panels for a person, plain text for agents and pipes."""
    if theme.styled():
        return FacetHelpFormatter
    return argparse.RawDescriptionHelpFormatter


def _with_examples(description: str, examples: tuple[str, ...]) -> str:
    """The description plus an ``examples:`` section of real command lines."""
    if not examples:
        return description
    lines = "\n".join(f"  {command}" for command in examples)
    return f"{description}\n\nexamples:\n{lines}"


def _group_help(group: argparse.ArgumentParser) -> Handler:
    """A group verb with no sub-verb: print its help and exit 2."""

    def handler(args: argparse.Namespace) -> int:
        if bool(getattr(args, "json", False)):
            error = FacetError("invalid_input", "the following arguments are required: VERB")
            contract.emit(contract.failure(error), True)
            return error.exit_code
        group.print_help(sys.stderr)
        return _USAGE_EXIT

    return handler


def _grouped_help() -> str:
    """The top-level epilog: the everyday verbs under three headings."""
    lines: list[str] = []
    for title, verbs in _VERB_GROUPS:
        lines.append(f"{title}:")
        for verb in verbs:
            verb_help = _VERB_HELP.get(verb) or _GROUP_HELP[verb]
            lines.append(f"  {verb:<9} {verb_help}")
        lines.append("")
    lines.append("maintainers: facet registry validate <registry-dir>")
    return "\n".join(lines)


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


def _add_verb(
    subparsers: argparse._SubParsersAction[_P],
    key: tuple[str, ...],
    help_text: str,
    handler: Handler,
    examples: Examples,
    lock: bool = False,
) -> argparse.ArgumentParser:
    """Add one verb; its key is ``(verb,)`` or ``(group, verb)``."""
    parser = subparsers.add_parser(
        key[-1],
        help=help_text,
        description=_with_examples(help_text, tuple(examples.get(key, ()))),
        formatter_class=_formatter_class(),
    )
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
    examples: Examples,
    handlers: Mapping[str, Handler],
    lock_verbs: frozenset[str] = frozenset(),
) -> dict[str, argparse.ArgumentParser]:
    """Add a group verb and its sub-verbs; no sub-verb prints the group's help."""
    group = subparsers.add_parser(
        name,
        help=help_text,
        description=_with_examples(help_text, tuple(examples.get((name,), ()))),
        formatter_class=_formatter_class(),
    )
    _add_json(group)
    nested = group.add_subparsers(
        dest=f"{name}_command", metavar="VERB", required=False, prog=group.prog
    )
    group.set_defaults(handler=_group_help(group))
    parsers: dict[str, argparse.ArgumentParser] = {}
    for verb, verb_help in verbs:
        parsers[verb] = _add_verb(
            nested,
            (name, verb),
            verb_help,
            handlers[verb],
            examples,
            lock=verb in lock_verbs,
        )
    return parsers


def _add_registry_group(
    subparsers: argparse._SubParsersAction[_P],
    examples: Examples,
    handler: Handler,
) -> None:
    group = subparsers.add_parser(
        "registry",
        help="inspect the package registry",
        description=_with_examples(
            "inspect the package registry (maintainers)",
            tuple(examples.get(("registry",), ())),
        ),
        formatter_class=_formatter_class(),
    )
    _add_json(group)
    nested = group.add_subparsers(
        dest="registry_command", metavar="VERB", required=False, prog=group.prog
    )
    group.set_defaults(handler=_group_help(group))
    validate = _add_verb(
        nested,
        ("registry", "validate"),
        "validate a registry checkout",
        handler,
        examples,
    )
    validate.add_argument(
        "dir",
        metavar="<registry-dir>",
        help="a registry checkout on this machine to validate",
    )


def _add_verbose_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=False,
        help="show the detailed working log on stderr instead of a progress line",
    )


def _add_update_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ref",
        default=None,
        metavar="REF",
        help="branch, tag, or commit to pin (default: $FACET_REGISTRY_REF or main)",
    )
    _add_verbose_flag(parser)


def _add_list_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--available",
        action="store_true",
        help="list every package in the registry snapshot, installed or not",
    )
    parser.add_argument(
        "--facet",
        default=None,
        choices=list(FACETS),
        metavar="FACET",
        help=f"list one facet's packages ({', '.join(FACETS)})",
    )


def _add_describe_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "capability",
        metavar="<capability>",
        help="a capability id such as ffmpeg.normalize, from `facet list --available`",
    )


def _add_skill_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "target",
        nargs="?",
        default=None,
        metavar="<package>",
        help=("a package name from `facet list`, or a capability id; omit it for the core skill"),
    )
    parser.add_argument(
        "--reference",
        default=None,
        metavar="REF",
        help="print one reference file of a package skill",
    )
    parser.add_argument(
        "--no-doctor",
        action="store_true",
        help="skip the quick doctor in the machine section",
    )


def _add_license_flags(parsers: dict[str, argparse.ArgumentParser]) -> None:
    accept = parsers["accept"]
    accept.add_argument(
        "model",
        nargs="?",
        default=None,
        metavar="<model-id>",
        help=(
            "a model id from `facet license list`; omit it at a terminal "
            "to choose from the pending ones"
        ),
    )
    accept.add_argument(
        "--yes",
        action="store_true",
        help="accept without asking; for agents directed by the machine's governor",
    )


def _add_config_flags(parsers: dict[str, argparse.ArgumentParser]) -> None:
    parsers["set"].add_argument(
        "key",
        metavar="<key>",
        help="a dotted host application key such as blender.path, from `facet config list`",
    )
    parsers["set"].add_argument(
        "value",
        metavar="<value>",
        help="the host application binary's path",
    )
    parsers["get"].add_argument(
        "key",
        metavar="<key>",
        help="a dotted host application key such as blender.path, from `facet config list`",
    )


def _add_pull_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "names",
        nargs="*",
        metavar="<facet-or-package>",
        help="a facet (image, video, …) or a package from `facet list`; repeatable",
    )
    parser.add_argument(
        "--package",
        action="append",
        default=[],
        metavar="NAME",
        help="install this package explicitly (repeatable; cannot mix with names)",
    )
    parser.add_argument(
        "--tier",
        metavar="NAME",
        help="prefer this tier within every tier group",
    )
    parser.add_argument(
        "--cpu",
        action="store_true",
        help="plan for a machine without a GPU",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="install a multi-package plan without asking",
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="print the plan and install nothing",
    )
    parser.add_argument(
        "--verify-models",
        action="store_true",
        help="recompute the sha256 of models that are already present",
    )
    _add_verbose_flag(parser)


def _add_check_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "asset",
        metavar="<asset>",
        help="an asset or its record, as `facet run` wrote them",
    )


def _add_doctor_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "target",
        nargs="?",
        default="",
        metavar="<capability-or-package>",
        help=(
            "a capability id or a package from `facet list`; "
            "omit it for facet and every installed package"
        ),
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="the quick depth of the package doctors (the default, about 10 s each)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="the full depth: starts each package's server and runs it once (up to 30 s each)",
    )


def _add_rm_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "package",
        metavar="<package>",
        help="an installed package name from `facet list`",
    )
    parser.add_argument(
        "--models",
        action="store_true",
        help="also delete the package's models that no other installed package lists",
    )


def _add_gc_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would be removed, and remove nothing",
    )


def _add_run_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "capability",
        metavar="<capability>",
        help="a capability id such as kokoro.speak, from `facet list --available`",
    )
    parser.add_argument(
        "piece",
        metavar="<piece.toml>",
        help=(
            "a piece TOML, as `facet example <capability> <folder>` writes, or - for JSON on stdin"
        ),
    )
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
    parser.add_argument(
        "--ephemeral",
        action="store_true",
        help="install the package for this run only, then remove its environment",
    )
    _add_verbose_flag(parser)


def _add_preview_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "asset",
        nargs="?",
        default=None,
        metavar="<asset-or-folder>",
        help="an asset, record or folder of assets (default: the current folder)",
    )
    parser.add_argument(
        "--kind",
        choices=sorted(preview.KINDS),
        default=None,
        help="folder mode only: keep the assets of this kind",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="print the preview URL without opening the system browser",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        metavar="PORT",
        help="port to serve the viewer on (default: a free port)",
    )
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=600.0,
        metavar="SECONDS",
        help="stop the server after this many seconds without requests (default: 600)",
    )


_FLAG_BUILDERS: dict[str, Callable[[argparse.ArgumentParser], None]] = {
    "update": _add_update_flags,
    "pull": _add_pull_flags,
    "list": _add_list_flags,
    "describe": _add_describe_flags,
    "example": example.add_flags,
    "run": _add_run_flags,
    "check": _add_check_flags,
    "preview": _add_preview_flags,
    "doctor": _add_doctor_flags,
    "remove": _add_rm_flags,
    "clean": _add_gc_flags,
    "skill": _add_skill_flags,
}


def build_parser(
    handlers: Mapping[str, Handler],
    license_handlers: Mapping[str, Handler],
    config_handlers: Mapping[str, Handler],
    registry_validate: Handler,
    examples: Examples,
) -> FacetParser:
    parser = FacetParser(
        prog="facet",
        description="Creation tools for agents: isolated, verifiable, license-aware.",
        usage="facet [-h] [--version] [--json] VERB ...",
        epilog=_grouped_help(),
        formatter_class=_formatter_class(),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    _add_json(parser)
    parser.set_defaults(json=False)
    subparsers = parser.add_subparsers(
        dest="command", metavar="VERB", required=True, help=argparse.SUPPRESS, prog="facet"
    )
    for verb in _VERBS:
        verb_parser = _add_verb(
            subparsers,
            (verb,),
            _VERB_HELP[verb],
            handlers[verb],
            examples,
            lock=verb in _LOCK_VERBS,
        )
        _FLAG_BUILDERS[verb](verb_parser)
    license_parsers = _add_group(
        subparsers,
        "license",
        _GROUP_HELP["license"],
        _LICENSE_VERBS,
        examples,
        license_handlers,
        lock_verbs=frozenset({"accept"}),
    )
    _add_license_flags(license_parsers)
    config_parsers = _add_group(
        subparsers,
        "config",
        _GROUP_HELP["config"],
        _CONFIG_VERBS,
        examples,
        config_handlers,
    )
    _add_config_flags(config_parsers)
    _add_registry_group(subparsers, examples, registry_validate)
    return parser
