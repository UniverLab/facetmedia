"""``facet license accept`` with no model: the wizard for a person at a terminal.

Everything here builds its own registry copy and drives the wizard with
scripted stdin and fake TTYs; nothing downloads.  ``_TtyProxy``, ``_TtyIn``,
``scripted_tty`` and ``make_installed`` are copied from
``tests.test_pull_wizard`` so this file reads alone.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
from pathlib import Path

import pytest

from facetmedia import license_accept
from facetmedia import licenses as licenses_module
from facetmedia.cli import main
from facetmedia.env.package_marker import package_fingerprint
from facetmedia.home import Home
from facetmedia.registry import load_registry

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry"
ECHO_MODEL = "echo-weights"
TERMSY_MODEL = "termsy-weights"
NO_PENDING = license_accept.NO_PENDING
# The list is printed once per pass, so an answer that returns to it prints it twice.
LISTINGS_AFTER_RETURN = 2
RETRIES = 3
EXIT_INVALID_INPUT = 2


class _TtyProxy:
    """Wrap a stream so ``isatty()`` is True while writes still get captured."""

    def __init__(self, wrapped: object) -> None:
        self._wrapped = wrapped

    def isatty(self) -> bool:
        return True

    def __getattr__(self, name: str) -> object:
        return getattr(self._wrapped, name)


class _TtyIn(io.StringIO):
    """A stdin whose only truth is ``isatty()``; answers come from ``input``."""

    def isatty(self) -> bool:
        return True


def scripted_tty(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> None:
    """Fake TTYs on both stdio and feed ``answers`` to ``input`` in order."""
    monkeypatch.setattr(sys, "stdin", _TtyIn(""))
    monkeypatch.setattr(sys, "stdout", _TtyProxy(sys.stdout))
    pending = list(answers)

    def fake_input(prompt: str = "") -> str:
        print(prompt, end="")
        return pending.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)


def make_installed(home: Home, registry_root: Path, name: str) -> None:
    """The markers an installed package carries, so the list sees it installed."""
    package = load_registry(registry_root).packages[name]
    env_dir = home.env_dir(package.name, package.version)
    env_dir.mkdir(parents=True)
    (env_dir / ".complete").write_text(package.lock_sha256, encoding="utf-8")
    (env_dir / ".package").write_text(package_fingerprint(package), encoding="utf-8")


def _append_echo_model(registry_copy: Path) -> None:
    """Add a second pending model, under a package that is not installed."""
    manifest = registry_copy / "packages" / "echo" / "package.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + f"""
[[models]]
id = "{ECHO_MODEL}"
spdx = "CC-BY-4.0"
commercial_use = true
requires_acceptance = true
terms_url = "https://example.invalid/echo-weights"
url = "file://tests/fixtures/models/termsy.bin"
sha256 = "85a73737b5d85eaba43e73ba33ceade28de49f1f4f7573377f02d58408f715c7"
bytes = 21
path = "echo-weights/termsy.bin"
""",
        encoding="utf-8",
    )


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The fixture registry plus ``echo-weights``, with ``termsy`` installed."""
    root = tmp_path / "registry"
    shutil.copytree(FIXTURES, root)
    _append_echo_model(root)
    monkeypatch.setenv("FACET_REGISTRY", str(root))
    make_installed(Home.peek_env(), root, "termsy")
    return root


def _records() -> list[dict[str, object]]:
    path = Home.peek_env().licenses_file
    if not path.is_file():
        return []
    accepted = json.loads(path.read_text(encoding="utf-8")).get("accepted")
    assert isinstance(accepted, list)
    return accepted


def _listing_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if line[:1].isdigit()]


def test_quit_shows_every_pending_model_and_records_nothing(
    registry: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted_tty(monkeypatch, ["q"])
    assert main(["license", "accept"]) == 0
    out = capsys.readouterr().out
    listed = [line for line in _listing_lines(out) if ")" in line]
    assert listed == [
        f"1) {ECHO_MODEL}  echo (not installed)  CC-BY-4.0  commercial use allowed",
        f"2) {TERMSY_MODEL}  termsy (installed)  CC-BY-NC-4.0  commercial use not allowed",
    ]
    assert license_accept.LIST_PROMPT in out
    assert _records() == []


def test_accepting_one_returns_to_the_shortened_list(
    registry: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted_tty(monkeypatch, ["2", TERMSY_MODEL, "q"])
    assert main(["license", "accept"]) == 0
    captured = capsys.readouterr()
    out = captured.out
    assert licenses_module.prompt_text(TERMSY_MODEL) in out
    for fragment in (
        f"model {TERMSY_MODEL}",
        "package termsy",
        "spdx CC-BY-NC-4.0",
        "commercial_use false",
        "terms_url https://example.invalid/termsy",
        licenses_module.NON_COMMERCIAL_LINE,
        licenses_module.RESPONSIBILITY_LINE,
    ):
        assert fragment in captured.err
    assert f"{TERMSY_MODEL} accepted" in out
    after = out.split(f"{TERMSY_MODEL} accepted")[1]
    assert f"1) {ECHO_MODEL}  echo (not installed)" in after
    assert TERMSY_MODEL not in after
    records = _records()
    assert [record["model"] for record in records] == [TERMSY_MODEL]
    assert records[0]["method"] == "interactive"


def test_mismatch_records_nothing_and_returns_to_the_list(
    registry: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted_tty(monkeypatch, ["1", "nope", "q"])
    assert main(["license", "accept"]) == 0
    out = capsys.readouterr().out
    assert "confirmation did not match" in out
    assert ECHO_MODEL in out.split("confirmation did not match")[0]
    assert out.count(f"1) {ECHO_MODEL}") == LISTINGS_AFTER_RETURN
    assert _records() == []


def test_enter_cancels_records_nothing_and_returns_to_the_list(
    registry: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted_tty(monkeypatch, ["1", "", "q"])
    assert main(["license", "accept"]) == 0
    out = capsys.readouterr().out
    assert licenses_module.CANCELLED_LINE in out
    assert out.count(f"1) {ECHO_MODEL}") == LISTINGS_AFTER_RETURN
    assert _records() == []


def test_a_bad_selection_asks_again(
    registry: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted_tty(monkeypatch, ["", "x", "7", "q"])
    assert main(["license", "accept"]) == 0
    out = capsys.readouterr().out
    assert out.count(license_accept.RETRY) == RETRIES
    assert out.count(f"1) {ECHO_MODEL}") == 1
    assert _records() == []


def test_nothing_pending_says_so_without_asking(
    registry: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert main(["license", "accept", TERMSY_MODEL, "--yes"]) == 0
    assert main(["license", "accept", ECHO_MODEL, "--yes"]) == 0
    capsys.readouterr()
    scripted_tty(monkeypatch, [])
    assert main(["license", "accept"]) == 0
    out = capsys.readouterr().out
    assert out == f"{NO_PENDING}\n"
    assert len(_records()) == LISTINGS_AFTER_RETURN


def test_no_model_off_a_terminal_names_the_direct_form(
    registry: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    assert main(["license", "accept", "--json"]) == EXIT_INVALID_INPUT
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "invalid_input"
    assert "facet license accept <model-id>" in error["message"]
    assert ECHO_MODEL in error["message"]
    assert TERMSY_MODEL in error["message"]
    assert _records() == []
