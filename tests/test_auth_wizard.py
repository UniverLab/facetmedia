"""``facet auth`` with no package: the wizard for a person at a terminal.

Bare ``facet auth`` lists the installed packages that declare the auth verb
and runs the existing sign-in flow for the chosen one. Everything here builds
its own registry and drives the wizard with fake TTYs: questionary prompts
read keystrokes from prompt_toolkit pipe input, one entry per prompt in
order. The runner is stubbed, so nothing downloads and no browser opens.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import questionary
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from facetmedia import auth as auth_module
from facetmedia import secrets
from facetmedia.cli import main
from facetmedia.contract import EXIT_CODES
from facetmedia.env.package_marker import package_fingerprint
from facetmedia.home import Home
from facetmedia.registry import load_active_registry, load_registry
from tests.test_pull_plan import Spec, write_registry

RUNNER_WITH_AUTH = '''"""Runner with an auth verb returning the declared secret."""
from __future__ import annotations
from typing import Any
from facet_runner_sdk import Request, Runner
runner = Runner()

@runner.verb("auth")
def auth(req: Request) -> dict[str, Any]:
    return {"secrets": {"token": "wizard-token"}}

@runner.verb("doctor")
def doctor(req: Request) -> dict[str, Any]:
    return {"checks": [{"name": "runner", "ok": True, "detail": "ready", "fix": ""}]}

runner.main()
'''

RUNNER_WITHOUT_AUTH = '''"""Runner with no auth verb."""
from __future__ import annotations
from typing import Any
from facet_runner_sdk import Request, Runner
runner = Runner()

@runner.verb("doctor")
def doctor(req: Request) -> dict[str, Any]:
    return {"checks": [{"name": "runner", "ok": True, "detail": "ready", "fix": ""}]}

runner.main()
'''

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
    """The markers an installed package carries, so the wizard sees it."""
    package = load_registry(registry_root).packages[name]
    env_dir = home.env_dir(package.name, package.version)
    env_dir.mkdir(parents=True)
    (env_dir / ".complete").write_text(package.lock_sha256, encoding="utf-8")
    (env_dir / ".package").write_text(package_fingerprint(package), encoding="utf-8")


def _write_runner(registry_root: Path, name: str, source: str) -> None:
    (registry_root / "packages" / name / "runner.py").write_text(source, encoding="utf-8")


def _append_secret(registry_root: Path, name: str, secret: str, description: str) -> None:
    manifest = registry_root / "packages" / name / "package.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + f'\n[[secrets]]\nname = "{secret}"\ndescription = "{description}"\nrequired = true\n',
        encoding="utf-8",
    )


def _auth_registry(use_registry, name: str, with_verb: bool) -> Path:
    root = use_registry([name])
    _append_secret(root, name, "token", "the test token")
    _write_runner(root, name, RUNNER_WITH_AUTH if with_verb else RUNNER_WITHOUT_AUTH)
    return root


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


def test_no_package_with_json_fails_missing_argument(use_registry, capsys) -> None:
    _auth_registry(use_registry, "a", True)
    assert main(["auth", "--json"]) == EXIT_INVALID_INPUT
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "invalid_input"
    assert error["message"] == "the following arguments are required: <package>"


def test_no_package_without_tty_refuses(use_registry, monkeypatch, capsys) -> None:
    _auth_registry(use_registry, "a", True)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert main(["auth"]) == EXIT_CONFIRMATION_REQUIRED
    assert EXIT_CODES["confirmation_required"] == EXIT_CONFIRMATION_REQUIRED
    captured = capsys.readouterr()
    assert "picking what to sign in to needs a terminal" in captured.err
    assert "facet auth <package>" in captured.err


def test_no_package_without_tty_json_refuses_with_envelope(
    use_registry, monkeypatch, capsys
) -> None:
    # --json wins over the TTY refusal: argparse's missing-argument message.
    _auth_registry(use_registry, "a", True)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert main(["auth", "--json"]) == EXIT_INVALID_INPUT
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["message"] == "the following arguments are required: <package>"


def test_wizard_picks_first_row_and_runs_handle(
    use_registry, monkeypatch, capsys, tmp_path
) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_installed(home, root, "a")
    monkeypatch.setattr("facetmedia.env.is_installed", lambda package, home: True)
    fake_outcome = SimpleNamespace(result={"secrets": {"token": "wizard-token"}})
    monkeypatch.setattr("facetmedia.runner.call", lambda inv, verb, request: fake_outcome)
    logged: list[str] = []
    monkeypatch.setattr("facetmedia.renewals.scrub", lambda run_id, values: logged.append(run_id))
    fake_tty(monkeypatch, ["\r"])
    assert main(["auth"]) == 0
    out = capsys.readouterr().out
    assert "a: stored token" in out
    assert "facet doctor a --full" in out
    values = secrets.env_for(home, load_active_registry().package("a"))
    assert values == {"FACET_SECRET_TOKEN": "wizard-token"}
    assert logged
    assert not list(home.tmp_dir.glob(".facet-auth-*"))
    assert "wizard-token" not in out


def test_eof_cancels_with_nothing_selected(use_registry, monkeypatch, capsys, tmp_path) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_installed(home, root, "a")
    monkeypatch.setattr("facetmedia.env.is_installed", lambda package, home: True)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(sys, "stdin", _TtyIn(""))
    monkeypatch.setattr(sys, "stdout", _TtyProxy(sys.stdout))
    monkeypatch.setattr("builtins.input", lambda _prompt="": (_ for _ in ()).throw(EOFError))
    assert main(["auth"]) == 0
    out = capsys.readouterr().out
    assert auth_module.NOTHING_SELECTED in out
    assert values_missing(home)


def values_missing(home: Home) -> bool:
    """No secret stored for the wizard package."""
    return secrets.env_for(home, load_active_registry().package("a")) == {}


def test_empty_registry_names_nothing_offered(use_registry, monkeypatch, capsys) -> None:
    root = use_registry(["a"])
    _append_secret(root, "a", "token", "the test token")
    _write_runner(root, "a", RUNNER_WITHOUT_AUTH)
    home = Home.from_env()
    make_installed(home, root, "a")
    monkeypatch.setattr("facetmedia.env.is_installed", lambda package, home: True)
    fake_tty(monkeypatch, [])
    assert main(["auth"]) == 0
    out = capsys.readouterr().out
    assert auth_module.NOTHING_OFFERED in out
    assert "facet pull" in out
    assert "facet secret set a token" in out
    assert values_missing(home)
