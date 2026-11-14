"""``facet license accept`` with no model: the wizard for a person at a terminal.

Everything here builds its own registry copy and drives the wizard with
fake TTYs: questionary prompts read keystrokes from prompt_toolkit pipe
input, one entry per prompt in order.  Nothing is recorded unless the typed
text is exactly the model id.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import sys
from pathlib import Path

import pytest
import questionary
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from facetmedia import license_accept
from facetmedia import licenses as licenses_module
from facetmedia.cli import main
from facetmedia.env.package_marker import package_fingerprint
from facetmedia.home import Home
from facetmedia.registry import load_active_registry, load_registry

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry"
ECHO_MODEL = "echo-weights"
TERMSY_MODEL = "termsy-weights"
NO_PENDING = license_accept.NO_PENDING
EXIT_INVALID_INPUT = 2
EXIT_CANCELLED = 130
PENDING_MODELS = 2
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def visible(text: str) -> str:
    """The text a person reads: the terminal styling removed."""
    return _ANSI.sub("", text)


class _TtyProxy:
    """Wrap a stream so ``isatty()`` is True while writes still get captured."""

    def __init__(self, wrapped: object) -> None:
        self._wrapped = wrapped

    def isatty(self) -> bool:
        return True

    def __getattr__(self, name: str) -> object:
        return getattr(self._wrapped, name)


class _TtyIn(io.StringIO):
    """A stdin whose only truth is ``isatty()``; prompts read pipe input."""

    def isatty(self) -> bool:
        return True


def fake_tty(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> None:
    """Fake TTYs on both stdio; each questionary prompt reads one pipe entry."""
    monkeypatch.setattr(sys, "stdin", _TtyIn(""))
    monkeypatch.setattr(sys, "stdout", _TtyProxy(sys.stdout))
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.delenv("NO_COLOR", raising=False)
    pending = list(answers)
    real = {name: getattr(questionary, name) for name in ("checkbox", "select", "confirm", "text")}

    def factory(name: str) -> object:
        def make(*args: object, **kwargs: object) -> questionary.Question:
            question = real[name](*args, **kwargs)
            entered = create_pipe_input()
            pipe = entered.__enter__()
            pipe.send_text(pending.pop(0))
            dummy = DummyOutput()
            question.application.input = pipe
            question.application.output = dummy
            question.application.renderer.output = dummy
            original = question.unsafe_ask

            def run(*args: object, **kwargs: object) -> object:
                try:
                    return original(*args, **kwargs)
                finally:
                    entered.__exit__(None, None, None)

            question.unsafe_ask = run  # type: ignore[method-assign]
            return question

        return make

    for name in real:
        monkeypatch.setattr(questionary, name, factory(name))


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


def test_model_line_shows_install_spdx_and_commercial(
    registry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Home.peek_env()
    active = load_active_registry()
    rows = {str(row["id"]): row for row in license_accept._pending()}
    echo = license_accept._model_line(rows[ECHO_MODEL], active, home)
    assert ECHO_MODEL in echo
    assert "echo (not installed)" in echo
    assert "CC-BY-4.0" in echo
    assert "commercial use allowed" in echo
    termsy = license_accept._model_line(rows[TERMSY_MODEL], active, home)
    assert TERMSY_MODEL in termsy
    assert "termsy (installed)" in termsy
    assert "commercial use not allowed" in termsy


def test_prompts_show_what_to_type_and_the_defaults() -> None:
    assert "[default: done]" in license_accept.SELECT_MESSAGE
    prompt = license_accept.type_prompt(TERMSY_MODEL)
    assert TERMSY_MODEL in prompt
    assert "[default: cancel]" in prompt


def test_done_records_nothing(
    registry: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_tty(monkeypatch, ["\x1b[B\x1b[B\r"])
    assert main(["license", "accept"]) == 0
    assert _records() == []


def test_accepting_one_returns_to_the_shortened_list(
    registry: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_tty(monkeypatch, ["\r", f"{ECHO_MODEL}\r", "\x1b[B\r"])
    assert main(["license", "accept"]) == 0
    captured = capsys.readouterr()
    out = visible(captured.out)
    assert f"Terms for {ECHO_MODEL}" in out
    assert "https://example.invalid/echo-weights" in out
    for fragment in (
        f"model {ECHO_MODEL}",
        "package echo",
        "spdx CC-BY-4.0",
        "commercial_use true",
        "terms_url https://example.invalid/echo-weights",
        licenses_module.RESPONSIBILITY_LINE,
    ):
        assert fragment in captured.err
    assert f"{ECHO_MODEL} accepted" in out
    records = _records()
    assert [record["model"] for record in records] == [ECHO_MODEL]
    assert records[0]["method"] == "interactive"


def test_mismatch_records_nothing_and_returns_to_the_list(
    registry: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_tty(monkeypatch, ["\r", "nope\r", "\x1b[B\x1b[B\r"])
    assert main(["license", "accept"]) == 0
    out = capsys.readouterr().out
    assert "confirmation did not match" in out
    assert f"Terms for {ECHO_MODEL}" in out
    assert _records() == []


def test_enter_cancels_records_nothing_and_returns_to_the_list(
    registry: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_tty(monkeypatch, ["\r", "\r", "\x1b[B\x1b[B\r"])
    assert main(["license", "accept"]) == 0
    out = capsys.readouterr().out
    assert licenses_module.CANCELLED_LINE in out
    assert _records() == []


def test_accepting_both_leaves_nothing_pending(
    registry: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_tty(monkeypatch, ["\r", f"{ECHO_MODEL}\r", "\r", f"{TERMSY_MODEL}\r"])
    assert main(["license", "accept"]) == 0
    out = visible(capsys.readouterr().out)
    assert f"{ECHO_MODEL} accepted" in out
    assert f"{TERMSY_MODEL} accepted" in out
    assert NO_PENDING in out
    assert len(_records()) == PENDING_MODELS


def test_ctrl_c_at_select_exits_130_and_records_nothing(
    registry: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_tty(monkeypatch, ["\x03"])
    assert main(["license", "accept"]) == EXIT_CANCELLED
    out = capsys.readouterr().out
    assert "cancelled" in out
    assert "Traceback" not in out
    assert _records() == []


def test_ctrl_c_at_typing_exits_130_and_records_nothing(
    registry: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_tty(monkeypatch, ["\r", "\x03"])
    assert main(["license", "accept"]) == EXIT_CANCELLED
    out = capsys.readouterr().out
    assert "cancelled" in out
    assert "Traceback" not in out
    assert _records() == []


def test_nothing_pending_says_so_without_asking(
    registry: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert main(["license", "accept", TERMSY_MODEL, "--yes"]) == 0
    assert main(["license", "accept", ECHO_MODEL, "--yes"]) == 0
    capsys.readouterr()
    fake_tty(monkeypatch, [])
    assert main(["license", "accept"]) == 0
    out = capsys.readouterr().out
    assert "█" in out
    assert out.endswith(f"{NO_PENDING}\n")
    assert len(_records()) == PENDING_MODELS


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
