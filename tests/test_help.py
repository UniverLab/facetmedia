"""The help contract: named placeholders, examples, groups and empty groups."""

from __future__ import annotations

import argparse
import json
import sys

import pytest

from facetmedia.cli import _EXAMPLES, build_parser, main
from facetmedia.cli_parser import (
    _CONFIG_VERBS,
    _GROUP_HELP,
    _LICENSE_VERBS,
    _VERB_HELP,
)

EXIT_USAGE = 2
MIN_EXAMPLES = 2
MAX_EXAMPLES = 4
GROUP_KEY_LEN = 2

PLACEHOLDERS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("pull",), "<facet-or-package>"),
    (("license", "accept"), "<model-id>"),
    (("describe",), "<capability>"),
    (("run",), "<capability>"),
    (("run",), "<piece.toml>"),
    (("example",), "<capability>"),
    (("example",), "<folder>"),
    (("check",), "<asset>"),
    (("preview",), "<asset-or-folder>"),
    (("remove",), "<package>"),
    (("skill",), "<package>"),
    (("registry", "validate"), "<registry-dir>"),
    (("doctor",), "<capability-or-package>"),
    (("config", "set"), "<key>"),
    (("config", "set"), "<value>"),
    (("config", "get"), "<key>"),
)


def _help(argv: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    """``facet *argv --help``: exit 0 and its standard output."""
    with pytest.raises(SystemExit) as exit_info:
        main([*argv, "--help"])
    assert exit_info.value.code == 0
    return capsys.readouterr().out


def _expected_keys() -> set[tuple[str, ...]]:
    sub_verbs = {
        (name, verb)
        for name, verbs in (("license", _LICENSE_VERBS), ("config", _CONFIG_VERBS))
        for verb, _help_text in verbs
    }
    groups = {(name,) for name in _GROUP_HELP} | {("registry",)}
    return {(verb,) for verb in _VERB_HELP} | sub_verbs | groups | {("registry", "validate")}


def _all_parsers(
    parser: argparse.ArgumentParser, path: tuple[str, ...] = ()
) -> list[tuple[str, argparse.ArgumentParser]]:
    """Every parser in the tree, keyed by the command that selects it."""
    found = [(" ".join(path), parser)]
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                found.extend(_all_parsers(child, (*path, name)))
    return found


@pytest.mark.parametrize("key", sorted(_EXAMPLES), ids="/".join)
def test_every_verb_help_ends_with_its_examples(
    key: tuple[str, ...], capsys: pytest.CaptureFixture[str]
) -> None:
    block = "examples:\n" + "\n".join(f"  {line}" for line in _EXAMPLES[key])
    out = _help(list(key), capsys)
    assert block in out
    assert f"{block}\n\n" in out


@pytest.mark.parametrize("key", sorted(_EXAMPLES), ids="/".join)
def test_examples_are_real_command_lines(key: tuple[str, ...]) -> None:
    lines = _EXAMPLES[key]
    assert MIN_EXAMPLES <= len(lines) <= MAX_EXAMPLES
    for line in lines:
        words = line.split()
        assert words[0] == "facet"
        assert words[1] == key[0]
        if len(key) == GROUP_KEY_LEN:
            assert words[2] == key[1]


def test_examples_table_covers_every_verb() -> None:
    assert set(_EXAMPLES) == _expected_keys()


def test_every_help_page_ends_with_examples() -> None:
    """Walk the parser tree: no verb, group or sub-verb may lack examples."""
    missing = [
        command
        for command, parser in _all_parsers(build_parser())
        if command and "examples:" not in parser.format_help()
    ]
    assert missing == []


@pytest.mark.parametrize(("key", "placeholder"), PLACEHOLDERS, ids="/".join)
def test_every_positional_placeholder_is_named(
    key: tuple[str, ...], placeholder: str, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _help(list(key), capsys)
    usage = out.split("\n\n", 1)[0]
    assert placeholder in usage


SOURCES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("pull",), "from `facet list`"),
    (("describe",), "from `facet list --available`"),
    (("run",), "from `facet list --available`"),
    (("run",), "`facet example <capability> <folder>` writes"),
    (("check",), "as `facet run` wrote them"),
    (("remove",), "from `facet list`"),
    (("skill",), "from `facet list`"),
    (("license", "accept"), "from `facet license list`"),
    (("registry", "validate"), "registry checkout"),
)


@pytest.mark.parametrize(("key", "phrase"), SOURCES, ids="/".join)
def test_positional_help_says_where_its_values_come_from(
    key: tuple[str, ...], phrase: str, capsys: pytest.CaptureFixture[str]
) -> None:
    out = " ".join(_help(list(key), capsys).split())
    assert phrase in out


def test_top_level_help_groups_the_everyday_verbs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _help([], capsys)
    assert out.startswith("usage: facet [-h] [--version] [--json] VERB ...")
    groups = [
        ("start", ("update", "pull", "list", "describe", "example")),
        ("make and check", ("run", "check", "preview")),
        ("maintain", ("doctor", "license", "config", "remove", "clean", "skill")),
    ]
    positions = [out.index(f"{title}:") for title, _verbs in groups]
    assert positions == sorted(positions)
    for title, verbs in groups:
        start = out.index(f"{title}:")
        end = out.index("\n\n", start)
        expected = "\n".join(
            f"  {verb:<9} {_VERB_HELP.get(verb) or _GROUP_HELP[verb]}" for verb in verbs
        )
        assert out[start:end] == f"{title}:\n{expected}"


def test_top_level_help_keeps_registry_on_the_maintainers_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _help([], capsys)
    assert "maintainers: facet registry validate <registry-dir>" in out
    assert "\n  registry " not in out


@pytest.mark.parametrize(
    ("group", "description"),
    [
        ("license", "manage tool licenses"),
        ("config", "set, get and list host application paths"),
        ("registry", "inspect the package registry (maintainers)"),
    ],
)
def test_group_without_a_sub_verb_prints_its_help(
    group: str, description: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([group]) == EXIT_USAGE
    err = capsys.readouterr().err
    assert f"usage: facet {group} " in err
    assert description in err
    assert "the following arguments are required" not in err


@pytest.mark.parametrize("group", ["license", "config", "registry"])
def test_group_without_a_sub_verb_keeps_the_json_failure(
    group: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([group, "--json"]) == EXIT_USAGE
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "invalid_input"
    assert "the following arguments are required: VERB" in envelope["error"]["message"]
    assert "usage:" not in captured.out


def test_parser_keeps_the_verb_arguments() -> None:
    args = build_parser().parse_args(["license", "accept", "omnivoice"])
    assert args.model == "omnivoice"
    assert args.json is False


class _TtyProxy:
    """Wrap a stream so ``isatty()`` is True while writes still get captured."""

    def __init__(self, wrapped: object) -> None:
        self._wrapped = wrapped

    def isatty(self) -> bool:
        return True

    def __getattr__(self, name: str) -> object:
        return getattr(self._wrapped, name)


def _styled_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """A styled TTY: stdout claims to be one, and ``NO_COLOR`` is unset."""
    monkeypatch.setattr(sys, "stdout", _TtyProxy(sys.stdout))
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.delenv("NO_COLOR", raising=False)


def _styled_help(argv: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    """``facet *argv --help`` on a styled TTY: exit 0 and its output."""
    with pytest.raises(SystemExit) as exit_info:
        main([*argv, "--help"])
    assert exit_info.value.code == 0
    return capsys.readouterr().out


def test_styled_top_help_renders_options_commands_and_group_panels(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _styled_tty(monkeypatch)
    out = _styled_help([], capsys)
    assert "Options" in out
    for title in ("start", "make and check", "maintain"):
        assert title in out
    assert "╭" in out
    for verb in ("update", "pull", "run", "doctor", "license", "skill"):
        assert verb in out
    assert "maintainers: facet registry validate <registry-dir>" in out


def test_styled_group_help_lists_sub_verbs_under_commands(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _styled_tty(monkeypatch)
    out = _styled_help(["license"], capsys)
    assert "Commands" in out
    assert "accept" in out
    assert "list" in out
    assert "examples:" in out


def test_styled_verb_help_keeps_every_flag(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _styled_tty(monkeypatch)
    out = _styled_help(["pull"], capsys)
    for flag in (
        "<facet-or-package>",
        "--package",
        "--tier",
        "--cpu",
        "--yes",
        "--plan",
        "--verify-models",
        "--json",
        "--no-wait",
        "--wait-timeout",
    ):
        assert flag in out
    assert "Options" in out
    assert "examples:" in out


def test_help_with_no_color_is_byte_identical_to_a_pipe(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.delenv("NO_COLOR", raising=False)
    argv_sets = ([], ["pull"], ["license", "accept"])
    plains = [_help(argv, capsys) for argv in argv_sets]
    _styled_tty(monkeypatch)
    monkeypatch.setenv("NO_COLOR", "1")
    for argv, plain in zip(argv_sets, plains, strict=True):
        styled = _styled_help(argv, capsys)
        assert styled == plain
        assert "╭" not in styled
