"""``facet auth login`` as a guided wizard for a person at a terminal.

Bare ``facet auth`` lists every installed package that declares a secret,
each with its stored state, and guides the multi-picked ones: typed secrets
first (echo off, with where to get them), then the package's browser
sign-in when it has one, then the full doctor check with one summary line
per package. Everything here builds its own registry and drives the wizard
with fake TTYs: questionary prompts read keystrokes from prompt_toolkit pipe
input, one entry per prompt in order. The runner and the doctor are stubbed,
so nothing downloads and no browser opens.

``facet auth login [<package>]`` is the same flow by name, and
``facet auth logout [<package>]`` removes a package's stored secrets on this
machine, choosing them at the terminal and never calling the network.
"""

from __future__ import annotations

import getpass
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
from facetmedia import auth_wizard as wizard_module
from facetmedia import secrets
from facetmedia.cli import main
from facetmedia.contract import EXIT_CODES, FacetError
from facetmedia.env.package_marker import package_fingerprint
from facetmedia.home import Home
from facetmedia.registry import load_active_registry, load_registry, validate_registry
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
    assert "facet secret set a token" in captured.err


def test_no_package_without_tty_json_refuses_with_envelope(
    use_registry, monkeypatch, capsys
) -> None:
    # --json wins over the TTY refusal: argparse's missing-argument message.
    _auth_registry(use_registry, "a", True)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert main(["auth", "--json"]) == EXIT_INVALID_INPUT
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["message"] == "the following arguments are required: <package>"


def values_missing(home: Home) -> bool:
    """No secret stored for the wizard package."""
    return secrets.env_for(home, load_active_registry().package("a")) == {}


def plain_prompts(monkeypatch: pytest.MonkeyPatch, inputs: list[str], entered: list[str]) -> None:
    """Drive the wizard off styling: ``input`` answers and ``getpass`` values."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(sys, "stdin", _TtyIn(""))
    monkeypatch.setattr(sys, "stdout", _TtyProxy(sys.stdout))
    answers = iter(inputs)

    def ask(prompt: object = "") -> str:
        print(prompt, end="")
        return next(answers)

    monkeypatch.setattr("builtins.input", ask)
    values = iter(entered)
    monkeypatch.setattr(getpass, "getpass", lambda _prompt="": next(values))


def failing_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every prompt raises: the wizard must never hang waiting for a person."""

    def fail(_prompt: object = "") -> object:
        raise AssertionError("prompted during a non-interactive path")

    monkeypatch.setattr("builtins.input", fail)
    monkeypatch.setattr(getpass, "getpass", fail)


def stub_runner(monkeypatch: pytest.MonkeyPatch, values: dict[str, str]) -> None:
    """The package runner returns the given secrets; nothing downloads."""
    fake_outcome = SimpleNamespace(result={"secrets": values})
    monkeypatch.setattr("facetmedia.runner.call", lambda inv, verb, request: fake_outcome)
    monkeypatch.setattr("facetmedia.renewals.scrub", lambda run_id, vals: None)


def stub_doctor(
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    secrets_ok: bool = True,
    detail: str = "",
    fix: str = "",
) -> None:
    """The full doctor reports the given exit code for the guided package."""

    def fake_execute(
        target: str, depth: str, run_id: str, wait: bool = True, timeout_s: float = 3600.0
    ) -> tuple[dict[str, object], int]:
        assert depth == "full"
        readiness: dict[str, object] = {
            "secrets": {"ok": secrets_ok, "detail": "token: set", "fix": ""}
        }
        if detail or fix or not secrets_ok or code != 0:
            readiness["models"] = {"ok": code == 0, "detail": detail, "fix": fix}
        data: dict[str, object] = {
            "healthy": code == 0,
            "facet": {},
            "packages": {
                target: {"installed": True, "readiness": readiness, "doctor": []},
            },
        }
        return data, code

    monkeypatch.setattr("facetmedia.doctor.execute", fake_execute)


def test_selection_lists_states_and_all_row(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_installed(home, root, "a")
    monkeypatch.setattr("facetmedia.env.is_installed", lambda package, home: True)
    stub_runner(monkeypatch, {"token": "wizard-token"})
    stub_doctor(monkeypatch, 0)
    plain_prompts(monkeypatch, ["1", ""], ["typed-token"])
    assert main(["auth"]) == 0
    out = capsys.readouterr().out
    assert "1. a" in out and "(not set)" in out
    assert "2. all" in out
    assert "3. done" in out
    assert "a: signed in" in out
    values = secrets.env_for(home, load_active_registry().package("a"))
    assert values == {"FACET_SECRET_TOKEN": "wizard-token"}
    assert "typed-token" not in out
    assert "wizard-token" not in out


def test_all_row_guides_every_package(use_registry, monkeypatch, capsys) -> None:
    root = use_registry(["a", "b"])
    _append_secret(root, "a", "token", "the test token")
    _append_secret(root, "a", "handle", "the account handle")
    _write_runner(root, "a", RUNNER_WITH_AUTH)
    _append_secret(root, "b", "token", "the other token")
    _write_runner(root, "b", RUNNER_WITHOUT_AUTH)
    home = Home.from_env()
    make_installed(home, root, "a")
    make_installed(home, root, "b")
    monkeypatch.setattr("facetmedia.env.is_installed", lambda package, home: True)
    stub_runner(monkeypatch, {"token": "wizard-token", "handle": "wizard-handle"})
    stub_doctor(monkeypatch, 0)
    plain_prompts(monkeypatch, ["all"], ["t", "h", "b-token"])
    assert main(["auth"]) == 0
    out = capsys.readouterr().out
    assert "a: signed in" in out
    assert "b: signed in" in out
    assert secrets.env_for(home, load_active_registry().package("b")) == {
        "FACET_SECRET_TOKEN": "b-token"
    }


def test_styled_checkbox_picks_the_toggled_package(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_installed(home, root, "a")
    monkeypatch.setattr("facetmedia.env.is_installed", lambda package, home: True)
    stub_runner(monkeypatch, {"token": "wizard-token"})
    stub_doctor(monkeypatch, 0)
    entered = iter(["typed-token"])
    monkeypatch.setattr(getpass, "getpass", lambda _prompt="": next(entered))
    # One space toggles the highlighted row (a); enter submits the selection.
    fake_tty(monkeypatch, [" \r"])
    assert main(["auth"]) == 0
    out = capsys.readouterr().out
    assert "a: signed in" in out
    assert secrets.env_for(home, load_active_registry().package("a")) == {
        "FACET_SECRET_TOKEN": "wizard-token"
    }


def test_eof_leaves_with_nothing_changed(use_registry, monkeypatch, capsys) -> None:
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
    assert auth_module.NOTHING_CHANGED in out
    assert values_missing(home)


def test_ctrl_c_at_selection_leaves_with_nothing_changed(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_installed(home, root, "a")
    monkeypatch.setattr("facetmedia.env.is_installed", lambda package, home: True)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(sys, "stdin", _TtyIn(""))
    monkeypatch.setattr(sys, "stdout", _TtyProxy(sys.stdout))

    def interrupt(_prompt: object = "") -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupt)
    assert main(["auth"]) == 0
    assert auth_module.NOTHING_CHANGED in capsys.readouterr().out
    assert values_missing(home)


def test_package_without_secrets_is_not_offered(use_registry, monkeypatch, capsys) -> None:
    root = use_registry(["a"])
    _write_runner(root, "a", RUNNER_WITHOUT_AUTH)
    home = Home.from_env()
    make_installed(home, root, "a")
    monkeypatch.setattr("facetmedia.env.is_installed", lambda package, home: True)
    fake_tty(monkeypatch, [])
    assert main(["auth"]) == 0
    out = capsys.readouterr().out
    assert auth_module.NOTHING_WITH_SECRETS in out
    assert "facet pull" in out


# --- `facet auth login`


def test_login_with_a_package_runs_the_per_package_flow(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_installed(home, root, "a")
    stub_runner(monkeypatch, {"token": "wizard-token"})
    stub_doctor(monkeypatch, 0)
    plain_prompts(monkeypatch, [], ["typed-token"])
    assert main(["auth", "login", "a"]) == 0
    out = capsys.readouterr().out
    assert "a: stored token" in out
    assert "a: signed in" in out
    assert secrets.env_for(home, load_active_registry().package("a")) == {
        "FACET_SECRET_TOKEN": "wizard-token"
    }


def test_login_without_a_package_runs_the_guide(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_installed(home, root, "a")
    monkeypatch.setattr("facetmedia.env.is_installed", lambda package, home: True)
    stub_runner(monkeypatch, {"token": "wizard-token"})
    stub_doctor(monkeypatch, 0)
    plain_prompts(monkeypatch, ["1"], ["typed-token"])
    assert main(["auth", "login"]) == 0
    out = capsys.readouterr().out
    assert "a: stored token" in out
    assert "a: signed in" in out
    assert secrets.env_for(home, load_active_registry().package("a")) == {
        "FACET_SECRET_TOKEN": "wizard-token"
    }


def test_the_package_alias_and_the_login_verb_run_the_same_flow(
    use_registry, monkeypatch, capsys
) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_installed(home, root, "a")
    stub_runner(monkeypatch, {"token": "wizard-token"})
    stub_doctor(monkeypatch, 0)
    plain_prompts(monkeypatch, [""], ["one"])
    assert main(["auth", "a"]) == 0
    assert "a: signed in" in capsys.readouterr().out
    assert main(["auth", "login", "a"]) == 0
    assert "a: signed in" in capsys.readouterr().out


def test_two_packages_without_a_verb_word_is_invalid_input(
    use_registry, monkeypatch, capsys
) -> None:
    _auth_registry(use_registry, "a", True)
    assert main(["auth", "a", "b"]) == EXIT_INVALID_INPUT
    captured = capsys.readouterr()
    assert "takes no second argument" in captured.err
    assert "facet auth login <package>" in captured.err


# --- `facet auth logout`


def _logged_in(home: Home, root: Path, values: dict[str, str]) -> None:
    """Store the given secrets so logout has something to remove."""
    package = load_registry(root).packages["a"]
    for name, value in values.items():
        secrets.set_value(home, package, name, value)


def test_logout_removes_every_chosen_secret(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    _append_secret(root, "a", "handle", "the account handle")
    home = Home.from_env()
    make_installed(home, root, "a")
    _logged_in(home, root, {"token": "t", "handle": "h"})
    removed: list[str] = []
    real_remove = secrets.remove

    def spy(where: Home, package: object, name: str) -> dict[str, object]:
        removed.append(name)
        return real_remove(where, package, name)

    monkeypatch.setattr(secrets, "remove", spy)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(sys, "stdin", _TtyIn(""))
    monkeypatch.setattr(sys, "stdout", _TtyProxy(sys.stdout))
    monkeypatch.setattr("builtins.input", lambda _prompt="": "1, 2")
    assert main(["auth", "logout", "a"]) == 0
    out = capsys.readouterr().out
    assert removed == ["token", "handle"]
    assert "a: removed token, handle" in out
    assert "nothing was revoked on the server" in out
    assert "facet auth login a" in out
    assert secrets.env_for(home, load_registry(root).packages["a"]) == {}


def test_logout_with_no_choice_removes_nothing(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_installed(home, root, "a")
    _logged_in(home, root, {"token": "t"})
    monkeypatch.setattr(secrets, "remove", lambda *a, **k: pytest.fail("remove was called"))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(sys, "stdin", _TtyIn(""))
    monkeypatch.setattr(sys, "stdout", _TtyProxy(sys.stdout))
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")
    assert main(["auth", "logout", "a"]) == 0
    out = capsys.readouterr().out
    assert auth_module.NOTHING_SELECTED in out
    assert secrets.env_for(home, load_registry(root).packages["a"]) == {"FACET_SECRET_TOKEN": "t"}


def test_logout_with_nothing_stored_is_a_quiet_no_op(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_installed(home, root, "a")
    monkeypatch.setattr(secrets, "remove", lambda *a, **k: pytest.fail("remove was called"))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(sys, "stdin", _TtyIn(""))
    monkeypatch.setattr(sys, "stdout", _TtyProxy(sys.stdout))
    monkeypatch.setattr("builtins.input", lambda _p="": pytest.fail("prompted with nothing set"))
    assert main(["auth", "logout", "a"]) == 0
    out = capsys.readouterr().out
    assert "a: nothing stored" in out


def test_logout_without_a_package_offers_installed_packages(
    use_registry, monkeypatch, capsys
) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_installed(home, root, "a")
    _logged_in(home, root, {"token": "t"})
    monkeypatch.setattr("facetmedia.env.is_installed", lambda package, home: True)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(sys, "stdin", _TtyIn(""))
    monkeypatch.setattr(sys, "stdout", _TtyProxy(sys.stdout))
    answers = iter(["1", "1"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    assert main(["auth", "logout"]) == 0
    out = capsys.readouterr().out
    assert "a: removed token" in out
    assert secrets.env_for(home, load_registry(root).packages["a"]) == {}


def test_logout_without_a_package_and_no_tty_refuses(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    make_installed(Home.from_env(), root, "a")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert main(["auth", "logout"]) == EXIT_CONFIRMATION_REQUIRED
    captured = capsys.readouterr()
    assert "picking what to sign out of needs a terminal" in captured.err
    assert "facet auth logout <package>" in captured.err


def test_logout_with_json_and_no_package_is_invalid_input(
    use_registry, monkeypatch, capsys
) -> None:
    root = _auth_registry(use_registry, "a", True)
    make_installed(Home.from_env(), root, "a")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert main(["auth", "logout", "--json"]) == EXIT_INVALID_INPUT
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "invalid_input"
    assert error["message"] == "the following arguments are required: <package>"


def test_logout_with_a_package_under_json_never_prompts(use_registry, monkeypatch, capsys) -> None:
    # Removal still asks which credentials; --json refuses instead of guessing.
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_installed(home, root, "a")
    _logged_in(home, root, {"token": "t"})
    monkeypatch.setattr("builtins.input", lambda _p="": pytest.fail("prompted under --json"))
    assert main(["auth", "logout", "a", "--json"]) == EXIT_INVALID_INPUT
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "invalid_input"
    assert error["details"] == {"package": "a"}
    assert secrets.env_for(home, load_registry(root).packages["a"]) == {"FACET_SECRET_TOKEN": "t"}


def test_logout_the_done_row_removes_nothing(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    _append_secret(root, "a", "handle", "the account handle")
    home = Home.from_env()
    make_installed(home, root, "a")
    _logged_in(home, root, {"token": "t", "handle": "h"})
    monkeypatch.setattr(secrets, "remove", lambda *a, **k: pytest.fail("remove was called"))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(sys, "stdin", _TtyIn(""))
    monkeypatch.setattr(sys, "stdout", _TtyProxy(sys.stdout))
    # Rows are 1. token, 2. handle, 3. done.
    monkeypatch.setattr("builtins.input", lambda _prompt="": "3")
    assert main(["auth", "logout", "a"]) == 0
    assert auth_module.NOTHING_SELECTED in capsys.readouterr().out
    assert secrets.env_for(home, load_registry(root).packages["a"]) == {
        "FACET_SECRET_TOKEN": "t",
        "FACET_SECRET_HANDLE": "h",
    }


def test_login_without_a_package_under_json_is_invalid_input(
    use_registry, monkeypatch, capsys
) -> None:
    _auth_registry(use_registry, "a", True)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert main(["auth", "login", "--json"]) == EXIT_INVALID_INPUT
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["message"] == "the following arguments are required: <package>"


def test_login_keeps_no_wait_on_the_lock_path(use_registry, monkeypatch, capsys) -> None:
    # --no-wait and --wait-timeout still reach the lock on the login path.
    root = _auth_registry(use_registry, "a", True)
    make_installed(Home.from_env(), root, "a")
    seen: list[tuple[bool, float]] = []
    real_hold = auth_module.lock.hold

    def spy(mode: str, purpose: str, wait: bool, timeout_s: float) -> object:
        seen.append((wait, timeout_s))
        return real_hold(mode, purpose, wait, timeout_s)

    monkeypatch.setattr(auth_module.lock, "hold", spy)
    stub_runner(monkeypatch, {"token": "wizard-token"})
    stub_doctor(monkeypatch, 0)
    plain_prompts(monkeypatch, [], ["typed-token"])
    assert main(["auth", "login", "a", "--no-wait", "--wait-timeout", "5"]) == 0
    assert seen == [(False, 5.0), (False, 5.0)]
    assert "a: signed in" in capsys.readouterr().out


def test_logout_resolves_the_package_like_login(use_registry, monkeypatch, capsys) -> None:
    # A package with no auth verb is refused by the shared resolver.
    root = use_registry(["a"])
    _append_secret(root, "a", "token", "the test token")
    _write_runner(root, "a", RUNNER_WITHOUT_AUTH)
    make_installed(Home.from_env(), root, "a")
    assert main(["auth", "logout", "a"]) == EXIT_INVALID_INPUT
    assert "provides no auth verb" in capsys.readouterr().err


def test_logout_refuses_an_unknown_package(use_registry, monkeypatch, capsys) -> None:
    _auth_registry(use_registry, "a", True)
    assert main(["auth", "logout", "nope"]) == EXIT_INVALID_INPUT
    assert "unknown package: nope" in capsys.readouterr().err


def test_logout_styled_checkbox_toggles_the_chosen_secrets(
    use_registry, monkeypatch, capsys
) -> None:
    root = _auth_registry(use_registry, "a", True)
    _append_secret(root, "a", "handle", "the account handle")
    home = Home.from_env()
    make_installed(home, root, "a")
    _logged_in(home, root, {"token": "t", "handle": "h"})
    # One space toggles the highlighted row (token); enter submits the selection.
    fake_tty(monkeypatch, [" \r"])
    assert main(["auth", "logout", "a"]) == 0
    out = capsys.readouterr().out
    assert "a: removed token" in out
    assert secrets.env_for(home, load_registry(root).packages["a"]) == {"FACET_SECRET_HANDLE": "h"}


# --- the four labels


def _produced_registry(use_registry, name: str = "a") -> Path:
    """A package whose only secret the auth verb produces: no typed prompts."""
    root = use_registry([name])
    manifest = root / "packages" / name / "package.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + '\n[[secrets]]\nname = "token"\ndescription = "the test token"\n'
        + 'required = true\nproduced_by = "auth"\n',
        encoding="utf-8",
    )
    _write_runner(root, name, RUNNER_WITH_AUTH)
    return root


def test_signed_in_needs_a_zero_doctor(use_registry, monkeypatch, capsys) -> None:
    root = _produced_registry(use_registry)
    home = Home.from_env()
    make_installed(home, root, "a")
    stub_runner(monkeypatch, {"token": "wizard-token"})
    stub_doctor(monkeypatch, 0)
    plain_prompts(monkeypatch, [], [])
    assert main(["auth", "login", "a"]) == 0
    out = capsys.readouterr().out
    assert f"a: {wizard_module.SIGNED_IN}" in out
    assert secrets.env_for(home, load_active_registry().package("a")) == {
        "FACET_SECRET_TOKEN": "wizard-token"
    }


def test_partly_set_when_the_produced_secret_stays_unset(use_registry, monkeypatch, capsys) -> None:
    root = _produced_registry(use_registry)
    _append_secret(root, "a", "session", "the browser-minted session")
    manifest = root / "packages" / "a" / "package.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + 'produced_by = "auth"\n', encoding="utf-8"
    )
    home = Home.from_env()
    make_installed(home, root, "a")
    # The verb succeeds but mints nothing: the produced secret stays unset.
    stub_runner(monkeypatch, {})
    stub_doctor(monkeypatch, 1, secrets_ok=False)
    plain_prompts(monkeypatch, [], [])
    assert main(["auth", "login", "a"]) == 0
    out = capsys.readouterr().out
    assert f"a: {wizard_module.PARTLY_SET}" in out
    assert wizard_module.SIGNED_IN not in out


def test_failed_prints_detail_and_fix(use_registry, monkeypatch, capsys) -> None:
    root = _produced_registry(use_registry)
    home = Home.from_env()
    make_installed(home, root, "a")
    stub_runner(monkeypatch, {"token": "wizard-token"})
    stub_doctor(monkeypatch, 1, detail="missing: m1", fix="facet pull a")
    plain_prompts(monkeypatch, [], [])
    assert main(["auth", "login", "a"]) == 0
    out = capsys.readouterr().out
    assert f"a: {wizard_module.FAILED}" in out
    assert "missing: m1" in out
    assert "facet pull a" in out
    assert wizard_module.SIGNED_IN not in out


def test_skipped_when_the_person_skips_the_sign_in(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_installed(home, root, "a")

    def crash(inv: object, verb: str, request: object) -> object:
        raise FacetError("runner_failed", "the verb crashed")

    monkeypatch.setattr("facetmedia.runner.call", crash)
    stub_doctor(monkeypatch, 0)
    plain_prompts(monkeypatch, ["skip"], ["typed-token"])
    assert main(["auth", "login", "a"]) == 0
    out = capsys.readouterr().out
    assert f"a: {wizard_module.SKIPPED}" in out
    assert wizard_module.SIGNED_IN not in out
    # The typed secret stays stored even though the browser flow was skipped.
    assert secrets.env_for(home, load_active_registry().package("a")) == {
        "FACET_SECRET_TOKEN": "typed-token"
    }


# --- keep or replace, retry, quit, and no-echo reading


def test_keep_reuses_the_stored_value(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_installed(home, root, "a")
    _logged_in(home, root, {"token": "old"})
    plain_prompts(monkeypatch, [""], [])
    monkeypatch.setattr(
        getpass, "getpass", lambda _prompt="": pytest.fail("replace asked for a value")
    )
    stub_runner(monkeypatch, {"token": "wizard-token"})
    stub_doctor(monkeypatch, 0)
    assert main(["auth", "login", "a"]) == 0
    assert "a: signed in" in capsys.readouterr().out
    assert secrets.env_for(home, load_active_registry().package("a")) == {
        "FACET_SECRET_TOKEN": "wizard-token"
    }


def test_replace_stores_the_new_value(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_installed(home, root, "a")
    _logged_in(home, root, {"token": "old"})
    stub_runner(monkeypatch, {"token": "wizard-token"})
    stub_doctor(monkeypatch, 0)
    plain_prompts(monkeypatch, ["y"], ["brand-new"])
    assert main(["auth", "login", "a"]) == 0
    out = capsys.readouterr().out
    assert "a: signed in" in out
    assert "brand-new" not in out


def test_retry_runs_the_sign_in_again(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_installed(home, root, "a")
    calls: list[str] = []

    def flaky(inv: object, verb: str, request: object) -> object:
        calls.append(verb)
        if len(calls) == 1:
            raise FacetError("runner_failed", "the browser flow timed out")
        return SimpleNamespace(result={"secrets": {"token": "wizard-token"}})

    monkeypatch.setattr("facetmedia.runner.call", flaky)
    monkeypatch.setattr("facetmedia.renewals.scrub", lambda run_id, vals: None)
    stub_doctor(monkeypatch, 0)
    plain_prompts(monkeypatch, ["retry"], ["typed-token"])
    assert main(["auth", "login", "a"]) == 0
    out = capsys.readouterr().out
    assert "sign-in failed: the browser flow timed out" in out
    assert "retry, skip or quit" in out
    assert "a: signed in" in out
    assert calls == ["auth", "auth"]


def test_quit_stops_cleanly_keeping_stored_secrets(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_installed(home, root, "a")

    def crash(inv: object, verb: str, request: object) -> object:
        raise FacetError("runner_failed", "the browser flow timed out")

    monkeypatch.setattr("facetmedia.runner.call", crash)
    stub_doctor(monkeypatch, 0)
    plain_prompts(monkeypatch, ["quit"], ["typed-token"])
    assert main(["auth", "login", "a"]) == 0
    out = capsys.readouterr().out
    assert "stored token" in out
    assert "a: signed in" not in out
    assert "a: skipped" not in out
    assert secrets.env_for(home, load_active_registry().package("a")) == {
        "FACET_SECRET_TOKEN": "typed-token"
    }


def test_quit_reports_the_auth_produced_secrets_already_stored(
    use_registry, monkeypatch, capsys
) -> None:
    root = use_registry(["a", "b"])
    for name in ("a", "b"):
        manifest = root / "packages" / name / "package.toml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8")
            + f'\n[[secrets]]\nname = "{name}-token"\ndescription = "the test token"\n'
            + 'required = true\nproduced_by = "auth"\n',
            encoding="utf-8",
        )
        _write_runner(root, name, RUNNER_WITH_AUTH)
    home = Home.from_env()
    make_installed(home, root, "a")
    make_installed(home, root, "b")
    monkeypatch.setattr("facetmedia.env.is_installed", lambda package, home: True)

    calls: list[str] = []

    def flaky(inv: object, verb: str, request: object) -> object:
        calls.append(verb)
        if len(calls) == 1:
            return SimpleNamespace(result={"secrets": {"a-token": "a-value"}})
        raise FacetError("runner_failed", "the browser flow timed out")

    monkeypatch.setattr("facetmedia.runner.call", flaky)
    monkeypatch.setattr("facetmedia.renewals.scrub", lambda run_id, vals: None)
    stub_doctor(monkeypatch, 0)
    plain_prompts(monkeypatch, ["all", "quit"], [])
    assert main(["auth"]) == 0
    out = capsys.readouterr().out
    assert "a: signed in" in out
    # The quit report is the last line: the stubbed prompt echoes without a
    # newline, so the `stored ...` report shares the prompt's line.
    assert out.splitlines()[-1].endswith(": stored a-token")
    assert "a: skipped" not in out
    assert secrets.env_for(home, load_active_registry().package("a")) == {
        "FACET_SECRET_A_TOKEN": "a-value"
    }


def test_typed_prompt_names_description_and_origin(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    manifest = root / "packages" / "a" / "package.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + 'obtain = "the provider console"\n',
        encoding="utf-8",
    )
    home = Home.from_env()
    make_installed(home, root, "a")
    stub_runner(monkeypatch, {"token": "wizard-token"})
    stub_doctor(monkeypatch, 0)
    seen: list[str] = []
    plain_prompts(monkeypatch, [], ["s3cr3t"])
    real_getpass = getpass.getpass
    monkeypatch.setattr(
        getpass, "getpass", lambda prompt="": seen.append(str(prompt)) or real_getpass(prompt)
    )
    assert main(["auth", "login", "a"]) == 0
    out = capsys.readouterr().out
    assert "the test token" in out
    assert "the provider console" in out
    assert seen == ["a token: "]
    assert "s3cr3t" not in out


def test_explicit_package_with_json_runs_the_verb_once(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_installed(home, root, "a")
    calls: list[str] = []

    def counting(inv: object, verb: str, request: object) -> object:
        calls.append(verb)
        return SimpleNamespace(result={"secrets": {"token": "wizard-token"}})

    monkeypatch.setattr("facetmedia.runner.call", counting)
    monkeypatch.setattr("facetmedia.renewals.scrub", lambda run_id, vals: None)
    failing_prompts(monkeypatch)
    assert main(["auth", "a", "--json"]) == 0
    out = capsys.readouterr().out
    assert calls == ["auth"]
    data = json.loads(out)["data"]
    assert data["package"] == "a"
    assert data["secrets"] == ["token"]
    assert data["path"] == str(secrets.vault_file(home))
    assert secrets.env_for(home, load_active_registry().package("a")) == {
        "FACET_SECRET_TOKEN": "wizard-token"
    }


def test_explicit_package_off_a_terminal_runs_the_verb_once(
    use_registry, monkeypatch, capsys
) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_installed(home, root, "a")
    stub_runner(monkeypatch, {"token": "wizard-token"})
    monkeypatch.setattr(
        "facetmedia.doctor.execute",
        lambda *args, **kwargs: pytest.fail("the check runs in the wizard only"),
    )
    failing_prompts(monkeypatch)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert main(["auth", "login", "a"]) == 0
    out = capsys.readouterr().out
    assert "a: stored token" in out
    assert "signed in" not in out
    assert secrets.env_for(home, load_active_registry().package("a")) == {
        "FACET_SECRET_TOKEN": "wizard-token"
    }


# --- selection parsing


def test_parse_rows_supports_comma_lists_and_repeats() -> None:
    rows = [("a 0.1 (not set)", "a"), ("b 0.1 (set)", "b"), ("done", None)]
    assert auth_module._parse_rows("", rows) == []
    assert auth_module._parse_rows("1", rows) == ["a"]
    assert auth_module._parse_rows("1, 2", rows) == ["a", "b"]
    assert auth_module._parse_rows("2 2 1", rows) == ["b", "a"]
    assert auth_module._parse_rows("3", rows) == []
    assert auth_module._parse_rows("9", rows) == []


def test_expand_picked_handles_all_done_and_blank() -> None:
    assert auth_module._expand_picked([], ["a"]) == []
    assert auth_module._expand_picked([auth_module._DONE], ["a"]) == []
    assert auth_module._expand_picked(["all"], ["a", "b"]) == ["a", "b"]
    assert auth_module._expand_picked(["a", "all"], ["a", "b"]) == ["a", "b"]
    assert auth_module._expand_picked(["b"], ["a", "b"]) == ["b"]
    assert auth_module._expand_picked(["1"], ["a"]) == []


# --- manifest keys, rows and surfaces


def _secret_problems(root: Path) -> list[tuple[str, str]]:
    """The ``(key, problem)`` pairs the registry validation reports for secrets."""
    return [
        (problem["key"], problem["problem"])
        for problem in validate_registry(root)
        if problem["key"].startswith("secrets")
    ]


def test_manifest_accepts_obtain_and_produced_by(use_registry) -> None:
    root = _produced_registry(use_registry)
    assert _secret_problems(root) == []


def test_manifest_rejects_a_bad_produced_by(use_registry) -> None:
    root = use_registry(["a"])
    manifest = root / "packages" / "a" / "package.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + '\n[[secrets]]\nname = "token"\ndescription = "the test token"\n'
        + 'produced_by = "browser"\n',
        encoding="utf-8",
    )
    assert _secret_problems(root) == [("secrets[0].produced_by", "value")]


@pytest.mark.parametrize("obtain", ['""', '"%s"' % ("x" * 301), '"line one\\nline two"'])
def test_manifest_rejects_bad_obtain_values(use_registry, obtain: str) -> None:
    root = use_registry(["a"])
    manifest = root / "packages" / "a" / "package.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + '\n[[secrets]]\nname = "token"\ndescription = "the test token"\n'
        + f"obtain = {obtain}\n",
        encoding="utf-8",
    )
    assert _secret_problems(root) == [("secrets[0].obtain", "value")]


def test_rows_carry_obtain_and_produced_by(use_registry) -> None:
    root = _produced_registry(use_registry)
    home = Home.from_env()
    package = load_registry(root).packages["a"]
    (row,) = secrets.rows(home, [package])
    assert row["obtain"] is None
    assert row["produced_by"] == "auth"


def test_secret_list_prints_obtain_next_to_description(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    manifest = root / "packages" / "a" / "package.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + 'obtain = "the provider console"\n',
        encoding="utf-8",
    )
    assert main(["secret", "list", "a"]) == 0
    out = capsys.readouterr().out
    assert "the test token" in out
    assert "the provider console" in out
