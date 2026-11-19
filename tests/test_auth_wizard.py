"""``facet auth`` with no package: the wizard for a person at a terminal.

Bare ``facet auth`` lists the installed packages that declare the auth verb
and runs the existing sign-in flow for the chosen one. Everything here builds
its own registry and drives the wizard with fake TTYs: questionary prompts
read keystrokes from prompt_toolkit pipe input, one entry per prompt in
order. The runner is stubbed, so nothing downloads and no browser opens.

``facet auth login [<package>]`` is the same flow by name, and
``facet auth logout [<package>]`` removes a package's stored secrets on this
machine, choosing them at the terminal and never calling the network.
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


# --- `facet auth login`


def test_login_with_a_package_runs_the_same_flow(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_installed(home, root, "a")
    called: list[str] = []
    monkeypatch.setattr(
        auth_module,
        "handle",
        lambda name, run_id: (
            called.append(name)
            or {
                "package": name,
                "secrets": ["token"],
                "path": str(secrets.vault_file(home)),
            }
        ),
    )
    assert main(["auth", "login", "a"]) == 0
    assert called == ["a"]
    out = capsys.readouterr().out
    assert "a: stored token" in out
    # With an explicit package the wizard-only doctor reminder stays out.
    assert "facet doctor a --full" not in out


def test_login_without_a_package_runs_the_guide(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_installed(home, root, "a")
    monkeypatch.setattr("facetmedia.env.is_installed", lambda package, home: True)
    fake_outcome = SimpleNamespace(result={"secrets": {"token": "wizard-token"}})
    monkeypatch.setattr("facetmedia.runner.call", lambda inv, verb, request: fake_outcome)
    monkeypatch.setattr("facetmedia.renewals.scrub", lambda run_id, values: None)
    fake_tty(monkeypatch, ["\r"])
    assert main(["auth", "login"]) == 0
    out = capsys.readouterr().out
    assert "a: stored token" in out
    assert "confirm with `facet doctor a --full`" in out
    assert secrets.env_for(home, load_active_registry().package("a")) == {
        "FACET_SECRET_TOKEN": "wizard-token"
    }


def test_the_package_alias_and_the_login_verb_run_the_same_flow(
    use_registry, monkeypatch, capsys
) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_installed(home, root, "a")
    handlers: list[str] = []

    def fake_handle(name: str, run_id: str) -> dict[str, object]:
        handlers.append(name)
        return {"package": name, "secrets": ["token"], "path": str(secrets.vault_file(home))}

    monkeypatch.setattr(auth_module, "handle", fake_handle)
    assert main(["auth", "a"]) == 0
    assert main(["auth", "login", "a"]) == 0
    capsys.readouterr()
    assert handlers == ["a", "a"]


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
    monkeypatch.setattr(
        auth_module,
        "handle",
        lambda name, run_id: {
            "package": name,
            "secrets": ["token"],
            "path": "x",
        },
    )
    assert main(["auth", "login", "a", "--no-wait", "--wait-timeout", "5"]) == 0
    assert seen == [(False, 5.0)]


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
