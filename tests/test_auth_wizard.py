"""``facet auth login`` as a guided wizard for a person at a terminal.

Bare ``facet auth`` offers every installed package that declares a secret
and is not fully signed in, one at a time with nothing preselected: typed
secrets first (masked echo, with where to get them, skipping stored ones
silently), then the package's browser sign-in when it has one, then the
full doctor check with one summary line per package. After each package the
menu shows again with the states recomputed, until the person picks done.
Everything here builds its own registry and drives the wizard with fake
TTYs: questionary prompts read keystrokes from prompt_toolkit pipe input,
one entry per prompt in order. The runner and the doctor are stubbed, so
nothing downloads and no browser opens.

``facet auth login [<package>]`` is the same flow by name, and
``facet auth logout [<package>]`` removes every stored secret of the
package at once on this machine, asking nothing and never calling the
network.
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
from facetmedia import installed as installed_module
from facetmedia import introspect, secrets
from facetmedia import masked_input as masked_module
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


def make_outdated(home: Home, registry_root: Path, name: str, old: str = "0.1.0") -> None:
    """An environment at ``old`` while the registry now holds 0.1.1."""
    env_dir = home.env_dir(name, old)
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / ".complete").write_text("old-lock", encoding="utf-8")
    (env_dir / ".package").write_text("old-package", encoding="utf-8")
    installed_module.record(home, name, old)
    manifest = registry_root / "packages" / name / "package.toml"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(
        text.replace(f'version = "{old}"', 'version = "0.1.1"', 1),
        encoding="utf-8",
    )


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
    """Drive the wizard off styling: ``input`` answers and masked secret values."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(sys, "stdin", _TtyIn(""))
    monkeypatch.setattr(sys, "stdout", _TtyProxy(sys.stdout))
    answers = iter(inputs)

    def ask(prompt: object = "") -> str:
        print(prompt, end="")
        return next(answers)

    monkeypatch.setattr("builtins.input", ask)
    values = iter(entered)
    monkeypatch.setattr("facetmedia.masked_input.read_masked", lambda _prompt="": next(values))


def failing_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every prompt raises: the wizard must never hang waiting for a person."""

    def fail(_prompt: object = "") -> object:
        raise AssertionError("prompted during a non-interactive path")

    monkeypatch.setattr("builtins.input", fail)
    monkeypatch.setattr(getpass, "getpass", fail)
    monkeypatch.setattr("facetmedia.masked_input.read_masked", fail)


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
    readiness: dict[str, object] | None = None,
    doctor_rows: list[object] | None = None,
) -> None:
    """The full doctor reports the given exit code for the guided package."""

    def fake_execute(
        target: str, depth: str, run_id: str, wait: bool = True, timeout_s: float = 3600.0
    ) -> tuple[dict[str, object], int]:
        assert depth == "full"
        if readiness is not None:
            pkg_readiness = readiness
        else:
            pkg_readiness = {"secrets": {"ok": secrets_ok, "detail": "token: set", "fix": ""}}
            if detail or fix or not secrets_ok or code != 0:
                pkg_readiness["models"] = {"ok": code == 0, "detail": detail, "fix": fix}
        data: dict[str, object] = {
            "healthy": code == 0,
            "facet": {},
            "packages": {
                target: {
                    "installed": True,
                    "readiness": pkg_readiness,
                    "doctor": doctor_rows if doctor_rows is not None else [],
                },
            },
        }
        return data, code

    monkeypatch.setattr("facetmedia.doctor.execute", fake_execute)


def test_selection_offers_one_package_with_done(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_installed(home, root, "a")
    monkeypatch.setattr("facetmedia.env.is_installed", lambda package, home: True)
    stub_runner(monkeypatch, {"token": "wizard-token"})
    stub_doctor(monkeypatch, 0)
    plain_prompts(monkeypatch, ["1"], ["typed-token"])
    assert main(["auth"]) == 0
    out = capsys.readouterr().out
    assert "1. a" in out
    assert "all" not in out
    assert "a: signed in" in out
    # The guided package is now set, so the loop finds nothing offered.
    assert auth_module.ALL_SIGNED_IN in out
    values = secrets.env_for(home, load_active_registry().package("a"))
    assert values == {"FACET_SECRET_TOKEN": "wizard-token"}
    assert "typed-token" not in out
    assert "wizard-token" not in out


def test_loop_guides_one_package_after_another(use_registry, monkeypatch, capsys) -> None:
    root = use_registry(["a", "b"])
    _append_secret(root, "a", "token", "the test token")
    _write_runner(root, "a", RUNNER_WITHOUT_AUTH)
    _append_secret(root, "b", "token", "the other token")
    _write_runner(root, "b", RUNNER_WITHOUT_AUTH)
    home = Home.from_env()
    make_installed(home, root, "a")
    make_installed(home, root, "b")
    monkeypatch.setattr("facetmedia.env.is_installed", lambda package, home: True)
    stub_doctor(monkeypatch, 0)
    plain_prompts(monkeypatch, ["1", "1"], ["a-token", "b-token"])
    assert main(["auth"]) == 0
    out = capsys.readouterr().out
    assert "a: signed in" in out
    assert "b: signed in" in out
    # The signed-in package drops out of the recomputed menu.
    assert out.count("1. a") == 1
    assert "1. b" in out
    assert auth_module.ALL_SIGNED_IN in out
    assert secrets.env_for(home, load_active_registry().package("a")) == {
        "FACET_SECRET_TOKEN": "a-token"
    }
    assert secrets.env_for(home, load_active_registry().package("b")) == {
        "FACET_SECRET_TOKEN": "b-token"
    }


def test_styled_select_picks_one_package(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_installed(home, root, "a")
    monkeypatch.setattr("facetmedia.env.is_installed", lambda package, home: True)
    stub_runner(monkeypatch, {"token": "wizard-token"})
    stub_doctor(monkeypatch, 0)
    entered = iter(["typed-token"])
    monkeypatch.setattr("facetmedia.masked_input.read_masked", lambda _prompt="": next(entered))
    # Enter submits the highlighted row (a); the guided package is then set,
    # so the recomputed menu offers nothing and the loop ends with no prompt.
    fake_tty(monkeypatch, ["\r"])
    assert main(["auth"]) == 0
    out = capsys.readouterr().out
    assert "a: signed in" in out
    assert auth_module.ALL_SIGNED_IN in out
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
    # The loop ends when the guided package leaves nothing offered.
    assert auth_module.ALL_SIGNED_IN in out
    assert secrets.env_for(home, load_active_registry().package("a")) == {
        "FACET_SECRET_TOKEN": "wizard-token"
    }


def test_all_signed_in_menu_names_sign_out(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_installed(home, root, "a")
    _logged_in(home, root, {"token": "t"})
    monkeypatch.setattr("facetmedia.env.is_installed", lambda package, home: True)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(sys, "stdin", _TtyIn(""))
    monkeypatch.setattr(sys, "stdout", _TtyProxy(sys.stdout))
    monkeypatch.setattr("builtins.input", lambda _p="": pytest.fail("prompted with none offered"))
    monkeypatch.setattr(getpass, "getpass", lambda _p="": pytest.fail("prompted with none offered"))
    monkeypatch.setattr(
        "facetmedia.masked_input.read_masked",
        lambda _p="": pytest.fail("prompted with none offered"),
    )
    assert main(["auth"]) == 0
    assert auth_module.ALL_SIGNED_IN in capsys.readouterr().out


def test_the_package_alias_and_the_login_verb_run_the_same_flow(
    use_registry, monkeypatch, capsys
) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_installed(home, root, "a")
    stub_runner(monkeypatch, {"token": "wizard-token"})
    stub_doctor(monkeypatch, 0)
    plain_prompts(monkeypatch, [], ["one"])
    assert main(["auth", "a"]) == 0
    assert "a: signed in" in capsys.readouterr().out
    assert main(["auth", "login", "a"]) == 0
    out = capsys.readouterr().out
    assert "a is already signed in; sign out first with facet auth logout a" in out
    assert "a: signed in" not in out


def test_named_login_on_a_signed_in_package_refuses_without_running(
    use_registry, monkeypatch, capsys
) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_installed(home, root, "a")
    _logged_in(home, root, {"token": "t"})
    monkeypatch.setattr(sys, "stdin", _TtyIn(""))
    monkeypatch.setattr(sys, "stdout", _TtyProxy(sys.stdout))
    monkeypatch.setattr("facetmedia.runner.call", lambda *a, **k: pytest.fail("the auth verb ran"))
    monkeypatch.setattr("builtins.input", lambda _p="": pytest.fail("prompted when signed in"))
    monkeypatch.setattr(getpass, "getpass", lambda _p="": pytest.fail("prompted when signed in"))
    monkeypatch.setattr(
        "facetmedia.masked_input.read_masked", lambda _p="": pytest.fail("prompted when signed in")
    )
    assert main(["auth", "login", "a"]) == 0
    out = capsys.readouterr().out
    assert "a is already signed in; sign out first with facet auth logout a" in out
    assert secrets.env_for(home, load_registry(root).packages["a"]) == {"FACET_SECRET_TOKEN": "t"}


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


def test_logout_removes_every_stored_secret_at_once(use_registry, monkeypatch, capsys) -> None:
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
    monkeypatch.setattr("builtins.input", lambda _prompt="": pytest.fail("asked which secrets"))
    monkeypatch.setattr(getpass, "getpass", lambda _prompt="": pytest.fail("asked which secrets"))
    monkeypatch.setattr(
        "facetmedia.masked_input.read_masked",
        lambda _prompt="": pytest.fail("asked which secrets"),
    )
    monkeypatch.setattr(
        "facetmedia.masked_input.read_masked",
        lambda _prompt="": pytest.fail("asked which secrets"),
    )
    assert main(["auth", "logout", "a"]) == 0
    out = capsys.readouterr().out
    assert removed == ["token", "handle"]
    assert "a: removed token, handle" in out
    assert "nothing was revoked on the server" in out
    assert "facet auth login a" in out
    assert secrets.env_for(home, load_registry(root).packages["a"]) == {}


def test_logout_removes_an_all_typed_package_without_questions(
    use_registry, monkeypatch, capsys
) -> None:
    root = use_registry(["x"])
    _append_secret(root, "x", "oauth-token", "the oauth token")
    _append_secret(root, "x", "oauth-secret", "the oauth secret")
    _write_runner(root, "x", RUNNER_WITH_AUTH)
    home = Home.from_env()
    make_installed(home, root, "x")
    package = load_registry(root).packages["x"]
    secrets.set_value(home, package, "oauth-token", "t")
    secrets.set_value(home, package, "oauth-secret", "s")
    monkeypatch.setattr("builtins.input", lambda _p="": pytest.fail("asked which secrets"))
    monkeypatch.setattr(getpass, "getpass", lambda _p="": pytest.fail("asked which secrets"))
    monkeypatch.setattr(questionary, "select", lambda *a, **k: pytest.fail("select was called"))
    monkeypatch.setattr(questionary, "checkbox", lambda *a, **k: pytest.fail("checkbox was called"))
    assert main(["auth", "logout", "x"]) == 0
    out = capsys.readouterr().out
    assert "x: removed oauth-token, oauth-secret" in out
    assert "nothing was revoked on the server" in out
    assert "facet auth login x" in out
    assert secrets.env_for(home, load_registry(root).packages["x"]) == {}


def test_logout_done_choice_removes_nothing(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_installed(home, root, "a")
    _logged_in(home, root, {"token": "t"})
    monkeypatch.setattr(secrets, "remove", lambda *a, **k: pytest.fail("remove was called"))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(sys, "stdin", _TtyIn(""))
    monkeypatch.setattr(sys, "stdout", _TtyProxy(sys.stdout))
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")
    assert main(["auth", "logout"]) == 0
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


def test_logout_with_a_package_under_json_removes_at_once(
    use_registry, monkeypatch, capsys
) -> None:
    # Nothing is asked, so --json removes and keeps its data shape.
    root = _auth_registry(use_registry, "a", True)
    _append_secret(root, "a", "handle", "the account handle")
    home = Home.from_env()
    make_installed(home, root, "a")
    _logged_in(home, root, {"token": "t", "handle": "h"})
    monkeypatch.setattr("builtins.input", lambda _p="": pytest.fail("prompted under --json"))
    assert main(["auth", "logout", "a", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["package"] == "a"
    assert data["removed"] == ["token", "handle"]
    assert data["path"] == str(secrets.vault_file(home))
    assert secrets.env_for(home, load_registry(root).packages["a"]) == {}


def test_logout_done_answer_removes_nothing(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    _append_secret(root, "a", "handle", "the account handle")
    home = Home.from_env()
    make_installed(home, root, "a")
    _logged_in(home, root, {"token": "t", "handle": "h"})
    monkeypatch.setattr(secrets, "remove", lambda *a, **k: pytest.fail("remove was called"))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(sys, "stdin", _TtyIn(""))
    monkeypatch.setattr(sys, "stdout", _TtyProxy(sys.stdout))
    monkeypatch.setattr("builtins.input", lambda _prompt="": "done")
    assert main(["auth", "logout"]) == 0
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


def test_logout_styled_removes_everything_without_asking(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    _append_secret(root, "a", "handle", "the account handle")
    home = Home.from_env()
    make_installed(home, root, "a")
    _logged_in(home, root, {"token": "t", "handle": "h"})
    # No prompt is offered, so no pipe entry is needed and none is consumed.
    fake_tty(monkeypatch, [])
    assert main(["auth", "logout", "a"]) == 0
    out = capsys.readouterr().out
    assert "a: removed token, handle" in out
    assert secrets.env_for(home, load_registry(root).packages["a"]) == {}


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
    assert "to start over: facet auth logout a" in out
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
    assert "to start over: facet auth logout a" in out
    assert out.index("to start over: facet auth logout a") < out.index("missing: m1")
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


def test_interrupted_signin_cancels_without_retry(
    use_registry, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _produced_registry(use_registry)
    home = Home.from_env()
    make_installed(home, root, "a")

    def interrupted(package_name: str, run_id: str, allow_open: bool = True) -> dict[str, object]:
        raise FacetError("runner_failed", "the auth run was interrupted; the runner was terminated")

    monkeypatch.setattr(auth_module, "handle", interrupted)
    stub_doctor(monkeypatch, 0)
    plain_prompts(monkeypatch, [], [])

    def fail_on_prompt(_prompt: object = "") -> str:
        raise AssertionError("retry offered after an interrupted sign-in")

    monkeypatch.setattr("builtins.input", fail_on_prompt)
    assert wizard_module.run_packages(["a"], run_id="run-1") == {}
    out = capsys.readouterr().out
    assert auth_module.NOTHING_CHANGED in out
    assert values_missing(home)


# --- stored values, stripping, retry, quit, and no-echo reading


def test_stored_typed_secret_is_kept_silently(use_registry, monkeypatch, capsys) -> None:
    root = use_registry(["a"])
    _append_secret(root, "a", "handle", "the account handle")
    manifest = root / "packages" / "a" / "package.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + '\n[[secrets]]\nname = "token"\ndescription = "the test token"\n'
        + 'required = true\nproduced_by = "auth"\n',
        encoding="utf-8",
    )
    _write_runner(root, "a", RUNNER_WITH_AUTH)
    home = Home.from_env()
    make_installed(home, root, "a")
    _logged_in(home, root, {"handle": "old"})
    plain_prompts(monkeypatch, [], [])
    monkeypatch.setattr(
        getpass, "getpass", lambda _prompt="": pytest.fail("asked for a stored value")
    )
    monkeypatch.setattr(
        "facetmedia.masked_input.read_masked",
        lambda _prompt="": pytest.fail("asked for a stored value"),
    )
    stub_runner(monkeypatch, {"token": "wizard-token"})
    stub_doctor(monkeypatch, 0)
    assert main(["auth", "login", "a"]) == 0
    out = capsys.readouterr().out
    assert "replace it?" not in out
    assert "a: signed in" in out
    assert secrets.env_for(home, load_active_registry().package("a")) == {
        "FACET_SECRET_HANDLE": "old",
        "FACET_SECRET_TOKEN": "wizard-token",
    }


def test_typed_value_is_stripped_before_storing(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", False)
    home = Home.from_env()
    make_installed(home, root, "a")
    stub_doctor(monkeypatch, 0)
    plain_prompts(monkeypatch, [], ["  spaced out  "])
    assert main(["auth", "login", "a"]) == 0
    out = capsys.readouterr().out
    assert "a: signed in" in out
    assert secrets.env_for(home, load_active_registry().package("a")) == {
        "FACET_SECRET_TOKEN": "spaced out"
    }
    assert "  spaced out  " not in out


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
    plain_prompts(monkeypatch, ["1", "1", "quit", ""], [])
    assert main(["auth"]) == 0
    out = capsys.readouterr().out
    assert "a: signed in" in out
    # Quitting b keeps a's stored secret in the cancelling report.
    assert "stored a-token" in out
    assert "b: signed in" not in out
    assert "b: skipped" not in out
    assert auth_module.NOTHING_CHANGED in out
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
    real_read_masked = masked_module.read_masked

    def recording(prompt: str = "") -> str:
        seen.append(str(prompt))
        return real_read_masked(prompt)

    monkeypatch.setattr(masked_module, "read_masked", recording)
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


# --- single-choice parsing


def test_match_answer_resolves_names_numbers_and_done() -> None:
    assert auth_module._match_answer("a", ["a", "b"]) == "a"
    assert auth_module._match_answer("2", ["a", "b"]) == "b"
    assert auth_module._match_answer("", ["a"]) is None
    assert auth_module._match_answer("done", ["a"]) is None
    assert auth_module._match_answer("zzz", ["a"]) is None
    assert auth_module._match_answer("9", ["a"]) is None
    assert auth_module._match_answer("1, 2", ["a", "b"]) is None


def test_pick_returns_one_name_not_a_list(use_registry, monkeypatch) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_installed(home, root, "a")
    monkeypatch.setattr(sys, "stdin", _TtyIn(""))
    monkeypatch.setattr(sys, "stdout", _TtyProxy(sys.stdout))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr("builtins.input", lambda _prompt="": "1")
    args = SimpleNamespace(json=False)
    picked = auth_module.pick(args)
    assert picked == "a"
    assert isinstance(picked, str)


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


SENAL_SGR = "38;2;232;197;71"
MUTED_SGR = "38;2;154;147;138"


def test_styled_wizard_marks_prompts_signed_in_and_hints(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", False)
    manifest = root / "packages" / "a" / "package.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + 'obtain = "the [provider] console"\n',
        encoding="utf-8",
    )
    home = Home.from_env()
    make_installed(home, root, "a")
    stub_doctor(monkeypatch, 1, detail="missing: m1", fix="facet pull a")
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.setattr(sys, "stdin", _TtyIn(""))
    monkeypatch.setattr(sys, "stdout", _TtyProxy(sys.stdout))
    monkeypatch.delenv("NO_COLOR", raising=False)
    entered = iter(["s3cr3t"])
    prompts: list[str] = []

    def fake_read_masked(prompt: str = "") -> str:
        prompts.append(prompt)
        return next(entered)

    monkeypatch.setattr("facetmedia.masked_input.read_masked", fake_read_masked)
    assert main(["auth", "login", "a"]) == 0
    out = capsys.readouterr().out
    assert prompts and SENAL_SGR in prompts[0]
    assert SENAL_SGR in out
    assert MUTED_SGR in out
    assert "a token:" in out
    assert "where to get it:" in out
    assert "the [provider] console" in out
    assert "to start over: facet auth logout a" in out
    assert "\x1b[1ma: failed\x1b[0m" in out


def test_styled_auth_menu_uses_accent_and_menu_style(use_registry, monkeypatch) -> None:
    root = _auth_registry(use_registry, "a", False)
    home = Home.from_env()
    make_installed(home, root, "a")
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.setattr(sys, "stdin", _TtyIn(""))
    monkeypatch.setattr(sys, "stdout", _TtyProxy(sys.stdout))
    monkeypatch.delenv("NO_COLOR", raising=False)
    captured: list[dict[str, object]] = []

    class _FakeQuestion:
        def unsafe_ask(self) -> object:
            from facetmedia import auth as auth_module  # noqa: PLC0415

            return auth_module._DONE

    def fake_select(message: str, **kwargs: object) -> _FakeQuestion:
        captured.append({"message": message, **kwargs})
        return _FakeQuestion()

    monkeypatch.setattr("facetmedia.auth.questionary.select", fake_select)
    assert main(["auth"]) == 0
    assert captured
    choices = captured[0]["choices"]
    assert isinstance(choices, list)
    assert choices[0].title[0] == ("class:accent", "a")
    style = captured[0]["style"]
    rules = dict(style.style_rules)  # type: ignore[attr-defined]
    assert rules["highlighted"] == "fg:#e8c547 bold"
    assert rules["pointer"] == "fg:#e8c547 bold"
    assert rules["selected"] == "fg:#e8c547"


def test_styled_wizard_marks_signed_in_with_senal(use_registry, monkeypatch, capsys) -> None:
    root = _produced_registry(use_registry)
    home = Home.from_env()
    make_installed(home, root, "a")
    stub_runner(monkeypatch, {"token": "wizard-token"})
    stub_doctor(monkeypatch, 0)
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.setattr(sys, "stdin", _TtyIn(""))
    monkeypatch.setattr(sys, "stdout", _TtyProxy(sys.stdout))
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert main(["auth", "login", "a"]) == 0
    out = capsys.readouterr().out
    assert "a: signed in" in out
    assert SENAL_SGR in out


def test_no_color_wizard_output_has_no_ansi(use_registry, monkeypatch, capsys) -> None:
    root = _produced_registry(use_registry)
    home = Home.from_env()
    make_installed(home, root, "a")
    stub_runner(monkeypatch, {"token": "wizard-token"})
    stub_doctor(monkeypatch, 0)
    plain_prompts(monkeypatch, [], [])
    assert main(["auth", "login", "a"]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "a: signed in" in out


def test_outdated_package_reports_signed_in_outdated(use_registry, monkeypatch, capsys) -> None:
    root = _produced_registry(use_registry)
    home = Home.from_env()
    make_installed(home, root, "a")
    stub_runner(monkeypatch, {"token": "wizard-token"})
    stub_doctor(
        monkeypatch,
        1,
        readiness={
            "secrets": {"ok": True},
            "installed": {"ok": False, "detail": "old", "fix": "other"},
        },
        doctor_rows=[],
    )
    plain_prompts(monkeypatch, [], [])
    assert main(["auth", "login", "a"]) == 0
    out = capsys.readouterr().out
    pull_line = "package out of date: facet pull a"
    label_line = f"a: {wizard_module.SIGNED_IN_OUTDATED}"
    assert pull_line in out
    assert label_line in out
    assert out.index(pull_line) < out.index(label_line)
    assert "to start over:" not in out
    assert "other" not in out


def test_outdated_plus_other_readiness_failure_stays_failed(
    use_registry, monkeypatch, capsys
) -> None:
    root = _produced_registry(use_registry)
    home = Home.from_env()
    make_installed(home, root, "a")
    stub_runner(monkeypatch, {"token": "wizard-token"})
    stub_doctor(
        monkeypatch,
        1,
        readiness={
            "secrets": {"ok": True},
            "installed": {"ok": False, "detail": "old", "fix": "facet pull a"},
            "models": {"ok": False, "detail": "missing", "fix": "facet pull a"},
        },
        doctor_rows=[],
    )
    plain_prompts(monkeypatch, [], [])
    assert main(["auth", "login", "a"]) == 0
    out = capsys.readouterr().out
    assert f"a: {wizard_module.FAILED}" in out
    assert "to start over:" in out
    assert "package out of date:" not in out


def test_outdated_plus_doctor_row_failure_stays_failed(use_registry, monkeypatch, capsys) -> None:
    root = _produced_registry(use_registry)
    home = Home.from_env()
    make_installed(home, root, "a")
    stub_runner(monkeypatch, {"token": "wizard-token"})
    stub_doctor(
        monkeypatch,
        1,
        readiness={
            "secrets": {"ok": True},
            "installed": {"ok": False, "detail": "old", "fix": "facet pull a"},
        },
        doctor_rows=[{"name": "runner", "ok": False, "detail": "d", "fix": "f"}],
    )
    plain_prompts(monkeypatch, [], [])
    assert main(["auth", "login", "a"]) == 0
    out = capsys.readouterr().out
    assert f"a: {wizard_module.FAILED}" in out
    assert "to start over:" in out
    assert "package out of date:" not in out


def test_outdated_with_unset_secrets_stays_partly_set(use_registry, monkeypatch, capsys) -> None:
    root = _produced_registry(use_registry)
    home = Home.from_env()
    make_installed(home, root, "a")
    stub_runner(monkeypatch, {"token": "wizard-token"})
    stub_doctor(
        monkeypatch,
        1,
        readiness={
            "secrets": {"ok": False, "detail": "missing", "fix": "facet secret set a token"},
            "installed": {"ok": False, "detail": "old", "fix": "facet pull a"},
        },
        doctor_rows=[],
    )
    plain_prompts(monkeypatch, [], [])
    assert main(["auth", "login", "a"]) == 0
    out = capsys.readouterr().out
    assert f"a: {wizard_module.PARTLY_SET}" in out
    assert "to start over:" in out
    assert "package out of date:" not in out


def test_styled_wizard_marks_signed_in_outdated_with_senal(
    use_registry, monkeypatch, capsys
) -> None:
    root = _produced_registry(use_registry)
    home = Home.from_env()
    make_installed(home, root, "a")
    stub_runner(monkeypatch, {"token": "wizard-token"})
    stub_doctor(
        monkeypatch,
        1,
        readiness={
            "secrets": {"ok": True},
            "installed": {"ok": False, "detail": "old", "fix": "facet pull a"},
        },
        doctor_rows=[],
    )
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.setattr(sys, "stdin", _TtyIn(""))
    monkeypatch.setattr(sys, "stdout", _TtyProxy(sys.stdout))
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert main(["auth", "login", "a"]) == 0
    out = capsys.readouterr().out
    assert f"a: {wizard_module.SIGNED_IN_OUTDATED}" in out
    assert SENAL_SGR in out


def test_outdated_login_menu_suffix(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    _append_secret(root, "a", "handle", "the account handle")
    home = Home.from_env()
    make_outdated(home, root, "a")
    package = load_registry(root).packages["a"]
    secrets.set_value(home, package, "token", "t")
    plain_prompts(monkeypatch, [""], [])
    assert main(["auth"]) == 0
    out = capsys.readouterr().out
    assert "1. a, outdated: facet pull a" in out


def test_outdated_logout_menu_suffix(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_outdated(home, root, "a")
    package = load_registry(root).packages["a"]
    secrets.set_value(home, package, "token", "t")
    plain_prompts(monkeypatch, [""], [])
    assert main(["auth", "logout"]) == 0
    out = capsys.readouterr().out
    assert "1. a, outdated: facet pull a" in out


def test_outdated_login_choice_pull_line(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    _append_secret(root, "a", "handle", "the account handle")
    home = Home.from_env()
    make_outdated(home, root, "a")
    package = load_registry(root).packages["a"]
    secrets.set_value(home, package, "token", "t")
    monkeypatch.setattr(
        "facetmedia.runner.call",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("runner called")),
    )
    plain_prompts(monkeypatch, ["1", ""], [])
    assert main(["auth"]) == 0
    out = capsys.readouterr().out
    assert "a is outdated; run facet pull a, then try again" in out
    assert out.count("1. a, outdated: facet pull a") > 1
    assert "nothing changed" in out


def test_outdated_logout_removes_secrets(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_outdated(home, root, "a")
    package = load_registry(root).packages["a"]
    secrets.set_value(home, package, "token", "t")
    assert main(["auth", "logout", "a"]) == 0
    out = capsys.readouterr().out
    assert "a: removed token" in out
    package = load_registry(root).packages["a"]
    assert secrets.env_for(home, package) == {}


def test_no_env_any_version_not_listed(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    package = load_registry(root).packages["a"]
    secrets.set_value(home, package, "token", "t")
    plain_prompts(monkeypatch, [""], [])
    assert main(["auth"]) == 0
    out = capsys.readouterr().out
    assert "1. a" not in out
    assert auth_module.NOTHING_WITH_SECRETS in out

    capsys.readouterr()
    plain_prompts(monkeypatch, [""], [])
    assert main(["auth", "logout"]) == 0
    out = capsys.readouterr().out
    assert "1. a" not in out
    assert auth_module.NOTHING_SELECTED in out


def test_outdated_named_login_prints_pull_line(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_outdated(home, root, "a")
    monkeypatch.setattr(
        "facetmedia.runner.call",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("runner called")),
    )
    plain_prompts(monkeypatch, [], [])
    assert main(["auth", "login", "a"]) == 0
    out = capsys.readouterr().out
    assert "a is outdated; run facet pull a, then try again" in out

    capsys.readouterr()
    assert main(["auth", "login", "a", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["data"] == {"package": "a", "outdated": True}


def test_outdated_logout_menu_removes_secrets(use_registry, monkeypatch, capsys) -> None:
    root = _auth_registry(use_registry, "a", True)
    home = Home.from_env()
    make_outdated(home, root, "a")
    package = load_registry(root).packages["a"]
    secrets.set_value(home, package, "token", "t")
    plain_prompts(monkeypatch, ["1"], [])
    assert main(["auth", "logout"]) == 0
    out = capsys.readouterr().out
    assert "1. a, outdated: facet pull a" in out
    assert "a: removed token" in out
    package = load_registry(root).packages["a"]
    assert secrets.env_for(home, package) == {}


# --- secret links

LINK = "https://console.example.com/keys/new"

CAPABILITY_TOML = """
[capabilities.text]
summary = "The test capability."
kind = "text"
models = []

[capabilities.text.preview]
viewer = "generic"
"""


def _linked_registry(use_registry, with_verb: bool = True) -> Path:
    """Package ``a`` with a typed secret carrying an obtain hint and a link."""
    root = _auth_registry(use_registry, "a", with_verb)
    manifest = root / "packages" / "a" / "package.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + 'obtain = "the provider console"\n'
        + f'link = "{LINK}"\n',
        encoding="utf-8",
    )
    return root


@pytest.mark.parametrize("link", [LINK, "https://example.com/" + "k" * 180])
def test_manifest_accepts_a_valid_link(use_registry, link: str) -> None:
    root = _auth_registry(use_registry, "a", True)
    manifest = root / "packages" / "a" / "package.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + f'link = "{link}"\n',
        encoding="utf-8",
    )
    assert _secret_problems(root) == []
    by_name = {secret.name: secret for secret in load_registry(root).packages["a"].secrets}
    assert by_name["token"].link == link


@pytest.mark.parametrize(
    "link",
    [
        '"http://console.example.com/keys"',
        '"https://console.example.com/my keys"',
        '"https://console.example.com/ke\\nys"',
        '"%s"' % ("https://example.com/" + "k" * 181),
    ],
)
def test_manifest_rejects_bad_link_values(use_registry, link: str) -> None:
    root = _auth_registry(use_registry, "a", True)
    manifest = root / "packages" / "a" / "package.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + f"link = {link}\n",
        encoding="utf-8",
    )
    assert _secret_problems(root) == [("secrets[0].link", "value")]


def test_typed_prompt_names_the_link_to_open(
    use_registry, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    root = _linked_registry(use_registry)
    home = Home.from_env()
    make_installed(home, root, "a")
    stub_runner(monkeypatch, {"token": "wizard-token"})
    stub_doctor(monkeypatch, 0)
    plain_prompts(monkeypatch, [], ["s3cr3t"])
    assert main(["auth", "login", "a"]) == 0
    out = capsys.readouterr().out
    assert "where to get it: the provider console" in out
    assert f"open: {LINK}" in out
    assert "s3cr3t" not in out


def test_typed_prompt_prints_no_open_line_without_a_link(
    use_registry, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
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
    plain_prompts(monkeypatch, [], ["s3cr3t"])
    assert main(["auth", "login", "a"]) == 0
    out = capsys.readouterr().out
    assert "where to get it: the provider console" in out
    assert "open:" not in out


def test_styled_wizard_marks_the_link_with_accent(
    use_registry, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    root = _linked_registry(use_registry, with_verb=False)
    home = Home.from_env()
    make_installed(home, root, "a")
    stub_doctor(monkeypatch, 1, detail="missing: m1", fix="facet pull a")
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.setattr(sys, "stdin", _TtyIn(""))
    monkeypatch.setattr(sys, "stdout", _TtyProxy(sys.stdout))
    monkeypatch.delenv("NO_COLOR", raising=False)
    entered = iter(["s3cr3t"])

    def fake_read_masked(prompt: str = "") -> str:
        return next(entered)

    monkeypatch.setattr("facetmedia.masked_input.read_masked", fake_read_masked)
    assert main(["auth", "login", "a"]) == 0
    out = capsys.readouterr().out
    assert "open:" in out
    assert LINK in out
    assert SENAL_SGR in out


def test_rows_secret_list_and_describe_carry_the_link(
    use_registry, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    root = _linked_registry(use_registry)
    manifest = root / "packages" / "a" / "package.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + CAPABILITY_TOML,
        encoding="utf-8",
    )
    home = Home.from_env()
    package = load_registry(root).packages["a"]
    (row,) = secrets.rows(home, [package])
    assert row["link"] == LINK
    assert main(["secret", "list", "a"]) == 0
    plain = capsys.readouterr().out
    assert "  obtain: the provider console" in plain
    assert f"  link: {LINK}" in plain
    assert main(["secret", "list", "a", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["secrets"] == [
        {
            "package": "a",
            "name": "token",
            "description": "the test token",
            "required": True,
            "set": False,
            "obtain": "the provider console",
            "link": LINK,
            "produced_by": None,
        }
    ]
    data = introspect.describe_data("a.text")
    assert [entry["link"] for entry in data["secrets"]] == [LINK]
