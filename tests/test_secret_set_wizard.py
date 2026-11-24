"""Interactive ``facet secret set`` with no arguments.

The wizard is exercised with fake TTYs and questionary pipe input, mirroring
``test_auth_wizard.py``. The value channel stays in ``cli._secret_value``.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
import questionary
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from facetmedia import secrets
from facetmedia.cli import main
from facetmedia.env.package_marker import package_fingerprint
from facetmedia.home import Home
from facetmedia.registry import load_active_registry, load_registry
from tests.test_pull_plan import Spec, write_registry

EXIT_INVALID_INPUT = 2
EXIT_CONFIRMATION_REQUIRED = 11


@pytest.fixture
def use_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point facet at a registry built inside the test."""

    def select(packages: list[str], spec: Spec | None = None) -> Path:
        root = write_registry(tmp_path / "registry", packages, spec)
        monkeypatch.setenv("FACET_REGISTRY", str(root))
        return root

    return select


def make_installed(home: Home, registry_root: Path, name: str) -> None:
    package = load_registry(registry_root).packages[name]
    env_dir = home.env_dir(package.name, package.version)
    env_dir.mkdir(parents=True)
    (env_dir / ".complete").write_text(package.lock_sha256, encoding="utf-8")
    (env_dir / ".package").write_text(package_fingerprint(package), encoding="utf-8")


def _append_secret(
    registry_root: Path, name: str, secret: str, description: str, *, required: bool = True
) -> None:
    manifest = registry_root / "packages" / name / "package.toml"
    req = "true" if required else "false"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + f'\n[[secrets]]\nname = "{secret}"\ndescription = "{description}"\nrequired = {req}\n',
        encoding="utf-8",
    )


class _TtyProxy:
    def __init__(self, wrapped: object) -> None:
        self._wrapped = wrapped

    def isatty(self) -> bool:
        return True

    def __getattr__(self, name: str) -> object:
        return getattr(self._wrapped, name)


class _TtyIn(io.StringIO):
    def isatty(self) -> bool:
        return True


def fake_tty(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> None:
    monkeypatch.setattr(sys, "stdin", _TtyIn(""))
    monkeypatch.setattr(sys, "stdout", _TtyProxy(sys.stdout))
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.delenv("NO_COLOR", raising=False)
    pending = list(answers)
    real = {name: getattr(questionary, name) for name in ("checkbox", "select", "confirm", "text")}

    def factory(name: str):
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

            def run(*a: object, **kw: object) -> object:
                try:
                    return original(*a, **kw)
                finally:
                    entered.__exit__(None, None, None)

            question.unsafe_ask = run  # type: ignore[method-assign]
            return question

        return make

    for name in real:
        monkeypatch.setattr(questionary, name, factory(name))


def _secret_registry(use_registry, name: str = "a", *, secrets_count: int = 2) -> Path:
    root = use_registry([name])
    for index in range(secrets_count):
        _append_secret(root, name, f"key{index}", f"secret {index}", required=True)
    return root


def test_guided_completion_stores_all_required(
    use_registry, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _secret_registry(use_registry, "a", secrets_count=2)
    home = Home.from_env()
    make_installed(home, root, "a")
    monkeypatch.setattr("facetmedia.env.is_installed", lambda package, home: True)
    values = iter(["val0", "val1"])
    monkeypatch.setattr("facetmedia.cli.getpass.getpass", lambda prompt: next(values))
    # pick package, pick key0, pick key1 -> all required set, exits
    fake_tty(monkeypatch, ["\r", "\r", "\r"])
    assert main(["secret", "set"]) == 0
    out = capsys.readouterr().out
    assert "a key0 set" in out
    assert "a key1 set" in out
    pkg = load_active_registry().package("a")
    assert secrets.env_for(home, pkg) == {
        "FACET_SECRET_KEY0": "val0",
        "FACET_SECRET_KEY1": "val1",
    }


def test_done_quits_zero(
    use_registry, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _secret_registry(use_registry, "a", secrets_count=1)
    home = Home.from_env()
    make_installed(home, root, "a")
    monkeypatch.setattr("facetmedia.env.is_installed", lambda package, home: True)
    # pick package, then pick done at secret menu
    fake_tty(monkeypatch, ["\r", "\x1b[B\r"])
    assert main(["secret", "set"]) == 0
    out = capsys.readouterr().out
    assert "nothing selected" in out
    pkg = load_active_registry().package("a")
    assert secrets.env_for(home, pkg) == {}


def test_undeclared_name_refused(
    use_registry, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _secret_registry(use_registry, "a", secrets_count=1)
    home = Home.from_env()
    make_installed(home, root, "a")
    monkeypatch.setattr("facetmedia.env.is_installed", lambda package, home: True)
    monkeypatch.setattr(sys, "stdin", _TtyIn(""))
    monkeypatch.setattr(sys, "stdout", _TtyProxy(sys.stdout))
    monkeypatch.setattr("facetmedia.cli.getpass.getpass", lambda prompt: "x")
    assert main(["secret", "set", "a", "nope", "--json"]) == EXIT_INVALID_INPUT
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "invalid_input"
    assert "declares no secret nope" in error["message"]


def test_empty_value_refused(
    use_registry, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _secret_registry(use_registry, "a", secrets_count=1)
    home = Home.from_env()
    make_installed(home, root, "a")
    monkeypatch.setattr("facetmedia.env.is_installed", lambda package, home: True)
    monkeypatch.setattr(sys, "stdin", _TtyIn(""))
    monkeypatch.setattr(sys, "stdout", _TtyProxy(sys.stdout))
    monkeypatch.setattr("facetmedia.cli.getpass.getpass", lambda prompt: "")
    assert main(["secret", "set", "a", "key0", "--json"]) == EXIT_INVALID_INPUT
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "invalid_input"
    assert "the value is empty" in error["message"]


def test_json_no_args_keeps_missing_argument_error(
    use_registry, capsys: pytest.CaptureFixture[str]
) -> None:
    _secret_registry(use_registry, "a", secrets_count=1)
    assert main(["secret", "set", "--json"]) == EXIT_INVALID_INPUT
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "invalid_input"
    assert error["message"] == "the following arguments are required: <package> <name>"


def test_non_tty_no_args_confirmation_required(
    use_registry, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _secret_registry(use_registry, "a", secrets_count=1)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert main(["secret", "set"]) == EXIT_CONFIRMATION_REQUIRED
    captured = capsys.readouterr()
    assert "storing credentials needs a terminal" in captured.err
    assert "facet secret set <package> <name>" in captured.err


def test_eof_exits_zero(
    use_registry, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _secret_registry(use_registry, "a", secrets_count=1)
    home = Home.from_env()
    make_installed(home, root, "a")
    monkeypatch.setattr("facetmedia.env.is_installed", lambda package, home: True)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(sys, "stdin", _TtyIn(""))
    monkeypatch.setattr(sys, "stdout", _TtyProxy(sys.stdout))
    monkeypatch.setattr("builtins.input", lambda _prompt="": (_ for _ in ()).throw(EOFError))
    assert main(["secret", "set"]) == 0
    out = capsys.readouterr().out
    assert "nothing selected" in out


def test_secretless_package_exits_zero(
    use_registry, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = use_registry(["a"])
    home = Home.from_env()
    make_installed(home, root, "a")
    monkeypatch.setattr("facetmedia.env.is_installed", lambda package, home: True)
    fake_tty(monkeypatch, ["\r"])
    assert main(["secret", "set"]) == 0
    out = capsys.readouterr().out
    assert "no secrets declared" in out


SENAL_SGR = "38;2;232;197;71"


def test_styled_secret_wizard_uses_accent_menu_style(
    use_registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _secret_registry(use_registry, "a", secrets_count=1)
    home = Home.from_env()
    make_installed(home, root, "a")
    monkeypatch.setattr("facetmedia.env.is_installed", lambda package, home: True)
    calls: list[object] = []
    real_menu_style = __import__("facetmedia.theme", fromlist=["menu_style"]).menu_style

    def spy_menu_style() -> object:
        style = real_menu_style()
        calls.append(style)
        return style

    monkeypatch.setattr("facetmedia.theme.menu_style", spy_menu_style)
    fake_tty(monkeypatch, ["\r", "\x1b[B\r"])
    assert main(["secret", "set"]) == 0
    assert calls
    rules = dict(calls[0].style_rules)  # type: ignore[union-attr]
    assert rules["highlighted"] == "fg:#e8c547 bold"


def test_no_color_secret_wizard_output_has_no_ansi(
    use_registry, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _secret_registry(use_registry, "a", secrets_count=1)
    home = Home.from_env()
    make_installed(home, root, "a")
    monkeypatch.setattr("facetmedia.env.is_installed", lambda package, home: True)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(sys, "stdin", _TtyIn(""))
    monkeypatch.setattr(sys, "stdout", _TtyProxy(sys.stdout))
    monkeypatch.setattr("builtins.input", lambda _prompt="": (_ for _ in ()).throw(EOFError))
    assert main(["secret", "set"]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "nothing selected" in out
