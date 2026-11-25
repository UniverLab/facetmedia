"""Credentials: the encrypted vault, its CLI, migration, the run gate, auth, and the mask.

The store is one owner-only vault under ``<home>/secrets/``. The tests cover
the store, migration, the passphrase ladder and rotation, the three CLI verbs
plus ``facet secret passphrase``, the run gate, masking, auth, and where a
value must never appear.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from facetmedia import auth as auth_module
from facetmedia import doctor as doctor_module
from facetmedia import introspect, prune, runner, runner_sdk, secrets
from facetmedia.cli import build_parser, main
from facetmedia.contract import FacetError
from facetmedia.home import Home
from facetmedia.registry import load_registry, validate_registry
from facetmedia.registry_types import Package
from facetmedia.vault import save, try_load

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry"
PACKAGE_DIR = Path(__file__).resolve().parent / "fixtures" / "poster-package"
EXIT_INVALID_INPUT = 2
EXIT_MACHINE_CANNOT = 5
EXIT_RUNNER_FAILED = 9
STORE_MODE = 0o600
DIR_MODE = 0o700
SECRET_VALUE = "s3cret-app-password"

CRASHING_RUNNER = '''"""Poster run that prints its secret and fails."""
from __future__ import annotations
from typing import Any
from facet_runner_sdk import Request, Runner, secret
runner = Runner()

@runner.verb("run")
def run(req: Request) -> dict[str, Any]:
    print(f"posting with {secret('app-password')}")
    raise RuntimeError("the account rejected it")

@runner.verb("doctor")
def doctor(req: Request) -> dict[str, Any]:
    return {"checks": [{"name": "runner", "ok": True, "detail": "ready", "fix": ""}]}

runner.main()
'''

PRINTING_AUTH_RUNNER = '''"""Poster auth that echoes its fresh token."""
from __future__ import annotations
from typing import Any
from facet_runner_sdk import Request, Runner
runner = Runner()
FRESH_TOKEN = "fresh-oauth-token-9d2c"

@runner.verb("auth")
def auth(req: Request) -> dict[str, Any]:
    print(f"minted {FRESH_TOKEN} for this run")
    return {"secrets": {"app-password": FRESH_TOKEN, "handle": "labbrand"}}

@runner.verb("doctor")
def doctor(req: Request) -> dict[str, Any]:
    return {"checks": [{"name": "runner", "ok": True, "detail": "ready", "fix": ""}]}

runner.main()
'''

FRESH_TOKEN = "fresh-oauth-token-9d2c"

UNDECLARED_AUTH_RUNNER = '''"""Poster auth that returns an undeclared secret."""
from __future__ import annotations
from typing import Any
from facet_runner_sdk import Request, Runner
runner = Runner()

@runner.verb("auth")
def auth(req: Request) -> dict[str, Any]:
    return {"secrets": {"app-password": "ok", "smuggled": "no"}}

@runner.verb("doctor")
def doctor(req: Request) -> dict[str, Any]:
    return {"checks": [{"name": "runner", "ok": True, "detail": "ready", "fix": ""}]}

runner.main()
'''


def _compose_registry(target: Path) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    for name in ("index.toml", "viewers", "lib"):
        source = FIXTURES / name
        if source.is_dir():
            shutil.copytree(source, target / name)
        else:
            shutil.copy2(source, target / name)
    shutil.copytree(FIXTURES / "packages", target / "packages")
    shutil.copytree(PACKAGE_DIR, target / "packages" / "poster")
    index = target / "index.toml"
    index.write_text(
        index.read_text(encoding="utf-8") + '\n[[packages]]\nname = "poster"\n',
        encoding="utf-8",
    )
    return target


@pytest.fixture(scope="module")
def poster_seed(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("facet-home-poster")
    registry = _compose_registry(tmp_path_factory.mktemp("facet-registry-poster"))
    previous = {name: os.environ.get(name) for name in ("FACET_HOME", "FACET_REGISTRY")}
    os.environ["FACET_HOME"] = str(root)
    os.environ["FACET_REGISTRY"] = str(registry)
    try:
        assert main(["pull", "poster", "--json"]) == 0
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    return root, registry


@pytest.fixture
def home(
    poster_seed: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, _facet_home_tmp: None
) -> tuple[Home, Path]:
    root, registry = poster_seed
    monkeypatch.setenv("FACET_HOME", str(root))
    monkeypatch.setenv("FACET_REGISTRY", str(registry))
    return Home.from_env(), registry


@pytest.fixture
def scratch_registry(home: tuple[Home, Path], tmp_path: Path) -> tuple[Home, Path]:
    facet_home, _registry = home
    return facet_home, _compose_registry(tmp_path / "registry")


@pytest.fixture
def clean_store(
    home: tuple[Home, Path], monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Home, Path]]:
    facet_home, registry = home
    monkeypatch.delenv(secrets.PASSPHRASE_ENV, raising=False)
    vpath = secrets.vault_file(facet_home)
    vpath.unlink(missing_ok=True)
    sec = facet_home.root / "secrets"
    sec.mkdir(parents=True, exist_ok=True)
    for path in sec.glob("*.json"):
        path.unlink(missing_ok=True)
    yield facet_home, registry
    monkeypatch.delenv(secrets.PASSPHRASE_ENV, raising=False)
    vpath.unlink(missing_ok=True)
    for path in sec.glob("*.json"):
        path.unlink(missing_ok=True)


@pytest.fixture
def swap_poster_runner(home: tuple[Home, Path]) -> Iterator[Any]:
    target = home[0].root / "envs" / "poster" / "0.1.0" / "package" / "runner.py"
    original = target.read_text(encoding="utf-8")

    def swap(source: str) -> None:
        target.write_text(source, encoding="utf-8")

    yield swap
    target.write_text(original, encoding="utf-8")


def _poster(registry: Path) -> Package:
    package = load_registry(registry).packages["poster"]
    assert package is not None
    return package


def _envelope(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    captured = capsys.readouterr()
    parsed: Any = json.loads(captured.out)
    assert isinstance(parsed, dict)
    return parsed


def _stdin(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


# --- manifest declaration


def test_the_fixture_package_declares_its_secrets(clean_store: tuple[Home, Path]) -> None:
    package = _poster(clean_store[1])
    assert [(s.name, s.required) for s in package.secrets] == [
        ("app-password", True),
        ("handle", False),
    ]


def _append_secret(registry: Path, body: str) -> None:
    manifest = registry / "packages" / "poster" / "package.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + f"\n[[secrets]]\n{body}\n", encoding="utf-8"
    )


def test_manifest_rejects_a_duplicate_secret_name(scratch_registry: tuple[Home, Path]) -> None:
    _facet_home, registry = scratch_registry
    _append_secret(registry, 'name = "handle"\ndescription = "again"')
    problems = validate_registry(registry)
    assert {p["key"] for p in problems} == {"secrets[2].name"}


def test_manifest_rejects_a_malformed_secret_name(scratch_registry: tuple[Home, Path]) -> None:
    _facet_home, registry = scratch_registry
    _append_secret(registry, 'name = "Bad_Name"\ndescription = "x"')
    assert validate_registry(registry)[0]["key"] == "secrets[2].name"


def test_manifest_rejects_an_empty_description(scratch_registry: tuple[Home, Path]) -> None:
    _facet_home, registry = scratch_registry
    _append_secret(registry, 'name = "other"\ndescription = ""')
    assert validate_registry(registry)[0] == {
        "file": "packages/poster/package.toml",
        "key": "secrets[2].description",
        "problem": "value",
    }


def test_manifest_rejects_an_unknown_secret_key(scratch_registry: tuple[Home, Path]) -> None:
    _facet_home, registry = scratch_registry
    _append_secret(registry, 'name = "other"\ndescription = "x"\nvalue = "leak"')
    assert validate_registry(registry)[0]["key"] == "secrets[2].value"


def test_manifest_rejects_a_non_boolean_required(scratch_registry: tuple[Home, Path]) -> None:
    _facet_home, registry = scratch_registry
    _append_secret(registry, 'name = "other"\ndescription = "x"\nrequired = "yes"')
    assert validate_registry(registry)[0]["problem"] == "type"


def test_manifest_required_defaults_to_false(scratch_registry: tuple[Home, Path]) -> None:
    _facet_home, registry = scratch_registry
    _append_secret(registry, 'name = "other"\ndescription = "x"')
    package = load_registry(registry).packages["poster"]
    assert [(s.name, s.required) for s in package.secrets] == [
        ("app-password", True),
        ("handle", False),
        ("other", False),
    ]


# --- vault store


def test_the_vault_is_owner_only_and_outside_every_environment(
    clean_store: tuple[Home, Path],
) -> None:
    facet_home, registry = clean_store
    secrets.set_value(facet_home, _poster(registry), "app-password", SECRET_VALUE)
    store = secrets.vault_file(facet_home)
    assert store == facet_home.root / "secrets" / "vault.enc"
    assert stat.S_IMODE(store.stat().st_mode) == STORE_MODE
    assert stat.S_IMODE(store.parent.stat().st_mode) == DIR_MODE
    assert facet_home.envs_dir not in store.parents
    assert SECRET_VALUE.encode() not in store.read_bytes()


def test_roundtrip_and_multiple_packages(clean_store: tuple[Home, Path]) -> None:
    facet_home, registry = clean_store
    poster = _poster(registry)
    secrets.set_value(facet_home, poster, "app-password", SECRET_VALUE)
    secrets.set_value(facet_home, poster, "handle", "labbrand")
    assert secrets.env_for(facet_home, poster) == {
        "FACET_SECRET_APP_PASSWORD": SECRET_VALUE,
        "FACET_SECRET_HANDLE": "labbrand",
    }


def test_a_stored_secret_survives_remove_and_clean(clean_store: tuple[Home, Path]) -> None:
    facet_home, registry = clean_store
    package = _poster(registry)
    secrets.set_value(facet_home, package, "app-password", SECRET_VALUE)
    prune.rm_package(facet_home, "poster", with_models=True, registry=load_registry(registry))
    prune.gc_sweep(facet_home, load_registry(registry))
    assert secrets.env_for(facet_home, package) == {"FACET_SECRET_APP_PASSWORD": SECRET_VALUE}


def test_an_undeclared_name_is_refused(clean_store: tuple[Home, Path]) -> None:
    facet_home, registry = clean_store
    with pytest.raises(FacetError) as exc_info:
        secrets.set_value(facet_home, _poster(registry), "smuggled", "x")
    assert exc_info.value.code == "invalid_input"
    assert "declares no secret smuggled" in exc_info.value.message
    assert exc_info.value.details["declared"] == ["app-password", "handle"]
    assert not secrets.vault_file(facet_home).exists()


def test_an_empty_value_is_refused(clean_store: tuple[Home, Path]) -> None:
    facet_home, registry = clean_store
    with pytest.raises(FacetError) as exc_info:
        secrets.set_value(facet_home, _poster(registry), "app-password", "")
    assert exc_info.value.code == "invalid_input"


def test_remove_drops_only_the_named_value(clean_store: tuple[Home, Path]) -> None:
    facet_home, registry = clean_store
    package = _poster(registry)
    secrets.set_value(facet_home, package, "app-password", SECRET_VALUE)
    secrets.set_value(facet_home, package, "handle", "labbrand")
    removed = secrets.remove(facet_home, package, "app-password")
    assert removed["removed"] is True
    assert removed["path"] == str(secrets.vault_file(facet_home))
    assert secrets.env_for(facet_home, package) == {"FACET_SECRET_HANDLE": "labbrand"}


def test_removing_an_unset_secret_is_a_no_op(clean_store: tuple[Home, Path]) -> None:
    facet_home, registry = clean_store
    removed = secrets.remove(facet_home, _poster(registry), "app-password")
    assert removed["removed"] is False


def test_env_for_exports_only_the_owners_declared_set_secrets(
    clean_store: tuple[Home, Path],
) -> None:
    facet_home, registry = clean_store
    package = _poster(registry)
    secrets.set_value(facet_home, package, "app-password", SECRET_VALUE)
    assert secrets.env_for(facet_home, package) == {"FACET_SECRET_APP_PASSWORD": SECRET_VALUE}
    other = load_registry(registry).packages["echo"]
    assert secrets.env_for(facet_home, other) == {}
    assert not any(k.startswith("FACET_SECRET_") for k in runner.runner_env(facet_home, other))


def test_env_names_follow_the_manifest_spelling() -> None:
    assert secrets.env_name("app-password") == "FACET_SECRET_APP_PASSWORD"


def test_mask_replaces_every_occurrence() -> None:
    assert secrets.mask("a s3cret b s3cret", ("s3cret",)) == "a *** b ***"
    assert secrets.mask("nothing", ("",)) == "nothing"


def test_mask_masks_a_value_containing_another_value_whole() -> None:
    assert secrets.mask("token abcdef here", ("abc", "abcdef")) == "token *** here"


# --- migration


def test_migration_merges_legacy_files_and_deletes_them(clean_store: tuple[Home, Path]) -> None:
    facet_home, registry = clean_store
    sec = facet_home.root / "secrets"
    (sec / "poster.json").write_text(json.dumps({"app-password": SECRET_VALUE}), encoding="utf-8")
    (sec / "other.json").write_text(json.dumps({"token": "t"}), encoding="utf-8")
    # first use migrates
    assert secrets.env_for(facet_home, _poster(registry)) == {
        "FACET_SECRET_APP_PASSWORD": SECRET_VALUE
    }
    assert secrets.vault_file(facet_home).exists()
    assert not (sec / "poster.json").exists()
    assert not (sec / "other.json").exists()
    # values from all packages merged
    store = try_load(facet_home.root, "")
    assert store is not None and store["other"]["token"] == "t"


def test_interrupted_migration_loses_nothing(clean_store: tuple[Home, Path]) -> None:
    facet_home, registry = clean_store
    sec = facet_home.root / "secrets"
    (sec / "poster.json").write_text(json.dumps({"app-password": SECRET_VALUE}), encoding="utf-8")
    (sec / "other.json").write_text(json.dumps({"token": "t"}), encoding="utf-8")
    # simulate interrupted migration: vault written but legacy files remain
    save(facet_home.root, {"poster": {"app-password": SECRET_VALUE}, "other": {"token": "t"}}, "")
    assert (sec / "poster.json").exists()
    # opening works and a subsequent write can clean up; data is intact
    assert secrets.env_for(facet_home, _poster(registry)) == {
        "FACET_SECRET_APP_PASSWORD": SECRET_VALUE
    }
    secrets.set_value(facet_home, _poster(registry), "handle", "labbrand")
    assert (
        secrets.env_for(facet_home, _poster(registry))["FACET_SECRET_APP_PASSWORD"] == SECRET_VALUE
    )
    assert try_load(facet_home.root, "")["other"]["token"] == "t"


def test_open_drains_leftover_legacy_files(clean_store: tuple[Home, Path]) -> None:
    facet_home, registry = clean_store
    sec = facet_home.root / "secrets"
    (sec / "poster.json").write_text(json.dumps({"app-password": SECRET_VALUE}), encoding="utf-8")
    # simulate interrupted migration: vault written but the plain file remains
    save(facet_home.root, {"other": {"token": "t"}}, "")
    assert secrets.env_for(facet_home, _poster(registry)) == {
        "FACET_SECRET_APP_PASSWORD": SECRET_VALUE
    }
    assert list(sec.glob("*.json")) == []
    assert try_load(facet_home.root, "") == {
        "poster": {"app-password": SECRET_VALUE},
        "other": {"token": "t"},
    }


def test_open_drains_legacy_without_changing_the_key(
    clean_store: tuple[Home, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    facet_home, registry = clean_store
    sec = facet_home.root / "secrets"
    save(facet_home.root, {"poster": {"app-password": SECRET_VALUE}}, "pw")
    (sec / "other.json").write_text(json.dumps({"token": "t"}), encoding="utf-8")
    monkeypatch.setenv(secrets.PASSPHRASE_ENV, "pw")
    assert secrets.env_for(facet_home, _poster(registry)) == {
        "FACET_SECRET_APP_PASSWORD": SECRET_VALUE
    }
    assert list(sec.glob("*.json")) == []
    merged = try_load(facet_home.root, "pw")
    assert merged is not None and merged["other"]["token"] == "t"
    assert try_load(facet_home.root, "") is None


def test_migration_removes_valueless_plain_files(clean_store: tuple[Home, Path]) -> None:
    facet_home, registry = clean_store
    sec = facet_home.root / "secrets"
    (sec / "junk.json").write_text(json.dumps({"token": ""}), encoding="utf-8")
    assert secrets.env_for(facet_home, _poster(registry)) == {}
    assert list(sec.glob("*.json")) == []
    assert not secrets.vault_file(facet_home).exists()


# --- passphrase / unlock ladder


def test_env_passphrase_unlocks(
    clean_store: tuple[Home, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    facet_home, registry = clean_store
    save(facet_home.root, {"poster": {"app-password": SECRET_VALUE}}, "pw")
    monkeypatch.setenv(secrets.PASSPHRASE_ENV, "pw")
    assert secrets.env_for(facet_home, _poster(registry)) == {
        "FACET_SECRET_APP_PASSWORD": SECRET_VALUE
    }


def test_wrong_env_passphrase_fails_without_overwrite(
    clean_store: tuple[Home, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    facet_home, _registry = clean_store
    save(facet_home.root, {"poster": {"app-password": SECRET_VALUE}}, "pw")
    before = secrets.vault_file(facet_home).read_bytes()
    monkeypatch.setenv(secrets.PASSPHRASE_ENV, "nope")
    with pytest.raises(FacetError) as exc_info:
        secrets.rows(facet_home, [_poster(_registry)])
    assert exc_info.value.code == "machine_cannot"
    assert secrets.PASSPHRASE_ENV in exc_info.value.message
    assert secrets.vault_file(facet_home).read_bytes() == before


def test_no_terminal_no_env_fails_with_hint(
    clean_store: tuple[Home, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    facet_home, _registry = clean_store
    save(facet_home.root, {"poster": {"app-password": SECRET_VALUE}}, "pw")
    monkeypatch.delenv(secrets.PASSPHRASE_ENV, raising=False)
    _stdin(monkeypatch, "")
    with pytest.raises(FacetError) as exc_info:
        secrets.rows(facet_home, [_poster(_registry)])
    assert exc_info.value.code == "machine_cannot"
    assert secrets.PASSPHRASE_ENV in (exc_info.value.hint or "")


def test_tty_prompt_unlocks(
    clean_store: tuple[Home, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    facet_home, registry = clean_store
    save(facet_home.root, {"poster": {"app-password": SECRET_VALUE}}, "pw")
    monkeypatch.delenv(secrets.PASSPHRASE_ENV, raising=False)
    monkeypatch.setattr(sys, "stdin", _Terminal(""))
    monkeypatch.setattr("facetmedia.secrets.getpass.getpass", lambda prompt: "pw")
    assert secrets.env_for(facet_home, _poster(registry)) == {
        "FACET_SECRET_APP_PASSWORD": SECRET_VALUE
    }


def test_corrupt_vault_fails_without_overwrite(clean_store: tuple[Home, Path]) -> None:
    facet_home, _registry = clean_store
    vpath = secrets.vault_file(facet_home)
    vpath.parent.mkdir(parents=True, exist_ok=True)
    vpath.write_bytes(b"not-a-vault")
    before = vpath.read_bytes()
    with pytest.raises(FacetError) as exc_info:
        secrets.rows(facet_home, [_poster(_registry)])
    assert exc_info.value.code == "machine_cannot"
    assert vpath.read_bytes() == before


def test_passphrase_rotate_and_remove(
    clean_store: tuple[Home, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    facet_home, registry = clean_store
    secrets.set_value(facet_home, _poster(registry), "app-password", SECRET_VALUE)
    # set passphrase
    assert secrets.change_passphrase(facet_home, "pw")["protected"] is True
    monkeypatch.setenv(secrets.PASSPHRASE_ENV, "pw")
    assert secrets.env_for(facet_home, _poster(registry)) == {
        "FACET_SECRET_APP_PASSWORD": SECRET_VALUE
    }
    # change passphrase
    monkeypatch.setenv(secrets.PASSPHRASE_ENV, "pw")
    secrets.change_passphrase(facet_home, "pw2")
    monkeypatch.setenv(secrets.PASSPHRASE_ENV, "pw2")
    assert secrets.env_for(facet_home, _poster(registry)) == {
        "FACET_SECRET_APP_PASSWORD": SECRET_VALUE
    }
    # remove passphrase
    secrets.change_passphrase(facet_home, "")
    monkeypatch.delenv(secrets.PASSPHRASE_ENV, raising=False)
    assert secrets.env_for(facet_home, _poster(registry)) == {
        "FACET_SECRET_APP_PASSWORD": SECRET_VALUE
    }


# --- CLI verbs


def test_secret_set_reads_stdin_and_lists_without_the_value(
    clean_store: tuple[Home, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    facet_home, registry = clean_store
    _stdin(monkeypatch, f"{SECRET_VALUE}\n")
    assert main(["secret", "set", "poster", "app-password", "--json"]) == 0
    envelope = _envelope(capsys)
    assert envelope["data"]["path"] == str(secrets.vault_file(facet_home))
    assert SECRET_VALUE not in json.dumps(envelope)
    assert (
        secrets.env_for(facet_home, _poster(registry))["FACET_SECRET_APP_PASSWORD"] == SECRET_VALUE
    )
    assert main(["secret", "list", "poster", "--json"]) == 0
    listing = _envelope(capsys)
    assert SECRET_VALUE not in json.dumps(listing)
    rows = {r["name"]: r for r in listing["data"]["secrets"]}
    assert rows["app-password"]["set"] is True
    assert rows["handle"]["set"] is False


def test_secret_set_takes_no_value_positional(
    clean_store: tuple[Home, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["secret", "set", "poster", "app-password", SECRET_VALUE])
    assert exit_info.value.code == EXIT_INVALID_INPUT


def test_secret_set_prompts_hidden_at_a_terminal(
    clean_store: tuple[Home, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    facet_home, registry = clean_store
    monkeypatch.setattr(sys, "stdin", _Terminal(""))
    prompts: list[str] = []

    def fake_getpass(prompt: str) -> str:
        prompts.append(prompt)
        return SECRET_VALUE

    monkeypatch.setattr("facetmedia.cli.getpass.getpass", fake_getpass)
    assert main(["secret", "set", "poster", "app-password", "--json"]) == 0
    assert prompts == ["poster app-password: "]
    assert SECRET_VALUE not in json.dumps(_envelope(capsys))
    assert secrets.env_for(facet_home, _poster(registry)) == {
        "FACET_SECRET_APP_PASSWORD": SECRET_VALUE
    }


def test_secret_set_refuses_an_empty_stdin(
    clean_store: tuple[Home, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _stdin(monkeypatch, "\n")
    assert main(["secret", "set", "poster", "app-password", "--json"]) == EXIT_INVALID_INPUT
    assert _envelope(capsys)["error"]["code"] == "invalid_input"


def test_secret_rm_reports_and_forgets(
    clean_store: tuple[Home, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    facet_home, registry = clean_store
    secrets.set_value(facet_home, _poster(registry), "app-password", SECRET_VALUE)
    assert main(["secret", "rm", "poster", "app-password", "--json"]) == 0
    assert _envelope(capsys)["data"]["removed"] is True
    assert secrets.env_for(facet_home, _poster(registry)) == {}


def test_secret_list_covers_every_package_that_declares_one(
    clean_store: tuple[Home, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["secret", "list", "--json"]) == 0
    packages = {row["package"] for row in _envelope(capsys)["data"]["secrets"]}
    assert packages == {"poster"}


def test_an_unknown_package_is_refused(
    clean_store: tuple[Home, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["secret", "list", "nope", "--json"]) == EXIT_INVALID_INPUT
    assert "known" in _envelope(capsys)["error"]["details"]


def test_secret_passphrase_cli_sets_and_removes(
    clean_store: tuple[Home, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    facet_home, registry = clean_store
    secrets.set_value(facet_home, _poster(registry), "app-password", SECRET_VALUE)
    _stdin(monkeypatch, "pw\n")
    assert main(["secret", "passphrase", "--json"]) == 0
    assert _envelope(capsys)["data"]["protected"] is True
    monkeypatch.setenv(secrets.PASSPHRASE_ENV, "pw")
    assert secrets.env_for(facet_home, _poster(registry)) == {
        "FACET_SECRET_APP_PASSWORD": SECRET_VALUE
    }
    _stdin(monkeypatch, "\n")
    assert main(["secret", "passphrase", "--json"]) == 0
    assert _envelope(capsys)["data"]["protected"] is False
    monkeypatch.delenv(secrets.PASSPHRASE_ENV, raising=False)
    assert secrets.env_for(facet_home, _poster(registry)) == {
        "FACET_SECRET_APP_PASSWORD": SECRET_VALUE
    }


# --- run gate


def test_a_run_without_its_required_secret_fails_before_the_runner(
    clean_store: tuple[Home, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    piece = tmp_path / "piece.toml"
    piece.write_text('[inputs]\ntext = "hola"\n', encoding="utf-8")
    assert main(["run", "poster.text", str(piece), "--json"]) == EXIT_MACHINE_CANNOT
    error = _envelope(capsys)["error"]
    assert error["code"] == "machine_cannot"
    assert error["details"]["missing"] == ["app-password"]
    assert error["hint"] == "facet auth poster"
    assert list(tmp_path.glob("*.facet.json")) == []


def test_a_run_with_locked_vault_no_passphrase_fails_with_env_hint(
    clean_store: tuple[Home, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    facet_home, _registry = clean_store
    save(facet_home.root, {"poster": {"app-password": SECRET_VALUE}}, "pw")
    monkeypatch.delenv(secrets.PASSPHRASE_ENV, raising=False)
    _stdin(monkeypatch, "")
    piece = tmp_path / "piece.toml"
    piece.write_text('[inputs]\ntext = "hola"\n', encoding="utf-8")
    assert main(["run", "poster.text", str(piece), "--json"]) == EXIT_MACHINE_CANNOT
    error = _envelope(capsys)["error"]
    assert error["code"] == "machine_cannot"
    assert secrets.PASSPHRASE_ENV in (error["hint"] or "")


def test_the_gate_names_secret_set_when_there_is_no_auth_verb(
    clean_store: tuple[Home, Path],
) -> None:
    facet_home, registry = clean_store
    package = _poster(registry)
    no_auth = SimpleNamespace(**{**package.__dict__, "directory": facet_home.root})
    with pytest.raises(FacetError) as exc_info:
        secrets.require_ready(facet_home, no_auth)  # type: ignore[arg-type]
    assert exc_info.value.hint == "facet secret set poster app-password"


def test_the_gate_passes_once_the_secret_is_stored(
    clean_store: tuple[Home, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    facet_home, registry = clean_store
    secrets.set_value(facet_home, _poster(registry), "app-password", SECRET_VALUE)
    piece = tmp_path / "piece.toml"
    piece.write_text('[inputs]\ntext = "hola"\n', encoding="utf-8")
    assert main(["run", "poster.text", str(piece), "--json"]) == 0
    data = _envelope(capsys)["data"]
    assert data["assets"][0]["verification"]["status"] == "passed"
    assert (tmp_path / "post.txt").read_text(encoding="utf-8") == "hola"


# --- masking


def test_the_runner_output_is_masked_in_stderr_and_the_log(
    clean_store: tuple[Home, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    facet_home, registry = clean_store
    secrets.set_value(facet_home, _poster(registry), "app-password", SECRET_VALUE)
    piece = tmp_path / "piece.toml"
    piece.write_text('[inputs]\ntext = "hola"\n', encoding="utf-8")
    assert main(["run", "poster.text", str(piece)]) == 0
    captured = capsys.readouterr()
    assert SECRET_VALUE not in captured.err
    assert "posting" in captured.err and secrets.MASK in captured.err
    logs = list(facet_home.logs_dir.rglob("*.log"))
    assert logs
    assert SECRET_VALUE not in "".join(p.read_text(encoding="utf-8") for p in logs)


def test_a_failed_runs_kept_tail_is_masked(
    clean_store: tuple[Home, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    swap_poster_runner: Any,
) -> None:
    facet_home, registry = clean_store
    secrets.set_value(facet_home, _poster(registry), "app-password", SECRET_VALUE)
    swap_poster_runner(CRASHING_RUNNER)
    piece = tmp_path / "piece.toml"
    piece.write_text('[inputs]\ntext = "hola"\n', encoding="utf-8")
    assert main(["run", "poster.text", str(piece), "--json"]) == EXIT_RUNNER_FAILED
    envelope = json.dumps(_envelope(capsys))
    assert SECRET_VALUE not in envelope
    assert secrets.MASK in envelope


def test_a_run_never_sees_another_packages_secret(clean_store: tuple[Home, Path]) -> None:
    facet_home, registry = clean_store
    secrets.set_value(facet_home, _poster(registry), "app-password", SECRET_VALUE)
    env_vars = runner.runner_env(facet_home, load_registry(registry).packages["echo"])
    assert not [k for k in env_vars if k.startswith("FACET_SECRET_")]


# --- auth


def test_auth_stores_what_the_verb_returned(
    clean_store: tuple[Home, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    facet_home, registry = clean_store
    assert main(["auth", "poster", "--json"]) == 0
    envelope = _envelope(capsys)
    assert "returned-password" not in json.dumps(envelope)
    data = envelope["data"]
    assert data["package"] == "poster"
    assert data["secrets"] == ["app-password", "handle"]
    assert data["path"] == str(secrets.vault_file(facet_home))
    assert secrets.env_for(facet_home, _poster(registry)) == {
        "FACET_SECRET_APP_PASSWORD": "returned-password",
        "FACET_SECRET_HANDLE": "labbrand",
    }
    assert not list(facet_home.tmp_dir.glob(".facet-auth-*"))


def test_auth_scrubs_a_fresh_token_from_the_run_log(
    clean_store: tuple[Home, Path], capsys: pytest.CaptureFixture[str], swap_poster_runner: Any
) -> None:
    facet_home, registry = clean_store
    swap_poster_runner(PRINTING_AUTH_RUNNER)
    assert main(["auth", "poster", "--json"]) == 0
    envelope = _envelope(capsys)
    assert FRESH_TOKEN not in json.dumps(envelope)
    stored = secrets.env_for(facet_home, _poster(registry))
    assert stored["FACET_SECRET_APP_PASSWORD"] == FRESH_TOKEN
    logs = list(facet_home.logs_dir.rglob("*.log"))
    assert logs
    assert FRESH_TOKEN not in "".join(p.read_text(encoding="utf-8") for p in logs)
    latest = max(logs, key=lambda p: p.stat().st_mtime)
    assert secrets.MASK in latest.read_text(encoding="utf-8")


def test_auth_refuses_a_name_the_manifest_did_not_declare(
    clean_store: tuple[Home, Path], capsys: pytest.CaptureFixture[str], swap_poster_runner: Any
) -> None:
    facet_home, _registry = clean_store
    swap_poster_runner(UNDECLARED_AUTH_RUNNER)
    assert main(["auth", "poster", "--json"]) == EXIT_INVALID_INPUT
    error = _envelope(capsys)["error"]
    assert error["details"]["undeclared"] == ["smuggled"]
    assert not secrets.vault_file(facet_home).exists()
    assert not list(facet_home.tmp_dir.glob(".facet-auth-*"))


def test_auth_refuses_a_package_without_the_verb(clean_store: tuple[Home, Path]) -> None:
    with pytest.raises(FacetError) as exc_info:
        auth_module.handle("echo", "run-1")
    assert exc_info.value.code == "invalid_input"
    assert "provides no auth verb" in exc_info.value.message


def test_auth_refuses_a_package_that_is_not_installed(
    clean_store: tuple[Home, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("facetmedia.env.is_installed", lambda package, home: False)
    with pytest.raises(FacetError) as exc_info:
        auth_module.handle("poster", "run-2")
    assert exc_info.value.code == "not_installed"


# --- describe and doctor


def test_describe_reports_each_secret_and_never_its_value(clean_store: tuple[Home, Path]) -> None:
    facet_home, registry = clean_store
    secrets.set_value(facet_home, _poster(registry), "app-password", SECRET_VALUE)
    data = introspect.describe_data("poster.text")
    assert SECRET_VALUE not in json.dumps(data)
    assert data["secrets"] == [
        {
            "name": "app-password",
            "description": "The app password from the account's settings.",
            "required": True,
            "set": True,
            "obtain": None,
            "link": None,
            "produced_by": None,
        },
        {
            "name": "handle",
            "description": "The account handle to post as.",
            "required": False,
            "set": False,
            "obtain": None,
            "link": None,
            "produced_by": None,
        },
    ]


def test_doctor_reports_the_secret_status_and_the_fix(clean_store: tuple[Home, Path]) -> None:
    facet_home, registry = clean_store
    checks, _failures = doctor_module.machine_checks(_poster(registry), facet_home)
    assert checks["secrets"] == {
        "ok": False,
        "detail": "app-password: not set (required); handle: not set",
        "fix": "facet auth poster",
    }
    secrets.set_value(facet_home, _poster(registry), "app-password", SECRET_VALUE)
    checks, _failures = doctor_module.machine_checks(_poster(registry), facet_home)
    assert checks["secrets"]["ok"] is True
    assert SECRET_VALUE not in json.dumps(checks)


def test_a_package_without_secrets_reports_none(clean_store: tuple[Home, Path]) -> None:
    facet_home, registry = clean_store
    checks, _failures = doctor_module.machine_checks(
        load_registry(registry).packages["echo"], facet_home
    )
    assert checks["secrets"] == {"ok": True, "detail": "no secrets declared", "fix": ""}


def test_the_secret_check_never_gates_a_run() -> None:
    assert "secrets" in doctor_module.CHECK_ORDER
    assert "secrets" not in doctor_module.CAN_RUN_CHECKS


# --- SDK helper


def test_the_sdk_reads_a_declared_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACET_SECRET_APP_PASSWORD", SECRET_VALUE)
    assert runner_sdk.secret("app-password") == SECRET_VALUE


def test_the_sdk_refuses_an_unset_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FACET_SECRET_APP_PASSWORD", raising=False)
    with pytest.raises(KeyError, match="app-password"):
        runner_sdk.secret("app-password")


def test_has_auth_verb_finds_the_registration(clean_store: tuple[Home, Path]) -> None:
    _facet_home, registry = clean_store
    loaded = load_registry(registry)
    assert secrets.has_auth_verb(loaded.packages["poster"]) is True
    assert secrets.has_auth_verb(loaded.packages["echo"]) is False
    assert secrets.auth_hint(loaded.packages["echo"], "token") == "facet secret set echo token"


# --- secret links


class _TtyOut:
    """Wrap stdout so ``isatty()`` is True while writes still get captured."""

    def __init__(self, wrapped: object) -> None:
        self._wrapped = wrapped

    def isatty(self) -> bool:
        return True

    def __getattr__(self, name: str) -> object:
        return getattr(self._wrapped, name)


def _hinted_home(
    scratch_registry: tuple[Home, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Home, Path]:
    """A fresh home pointed at a scratch registry whose secret names its hints."""
    _facet_home, registry = scratch_registry
    manifest = registry / "packages" / "poster" / "package.toml"
    text = manifest.read_text(encoding="utf-8")
    text = text.replace(
        'name = "app-password"\n',
        'name = "app-password"\n'
        'obtain = "Account settings > Security"\n'
        'link = "https://account.example.com/security/app"\n',
        1,
    )
    manifest.write_text(text, encoding="utf-8")
    monkeypatch.setenv("FACET_HOME", str(tmp_path / "facet-home"))
    monkeypatch.setenv("FACET_REGISTRY", str(registry))
    monkeypatch.delenv(secrets.PASSPHRASE_ENV, raising=False)
    return Home.from_env(), registry


def _tty_set(monkeypatch: pytest.MonkeyPatch, value: str = SECRET_VALUE) -> None:
    """Both stdio are terminals; the masked prompt answers ``value``."""
    monkeypatch.setattr(sys, "stdin", _Terminal(""))
    monkeypatch.setattr(sys, "stdout", _TtyOut(sys.stdout))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr("facetmedia.masked_input.read_masked", lambda prompt="": value)


def test_secret_set_prints_hints_on_a_terminal(
    scratch_registry: tuple[Home, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    facet_home, registry = _hinted_home(scratch_registry, tmp_path, monkeypatch)
    _tty_set(monkeypatch)
    assert main(["secret", "set", "poster", "app-password"]) == 0
    out = capsys.readouterr().out
    assert "where to get it: Account settings > Security" in out
    assert "open: https://account.example.com/security/app" in out
    assert SECRET_VALUE not in out
    assert secrets.env_for(facet_home, _poster(registry)) == {
        "FACET_SECRET_APP_PASSWORD": SECRET_VALUE
    }


def test_secret_set_prints_no_hints_off_a_terminal(
    scratch_registry: tuple[Home, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _hinted_home(scratch_registry, tmp_path, monkeypatch)
    _stdin(monkeypatch, f"{SECRET_VALUE}\n")
    assert main(["secret", "set", "poster", "app-password"]) == 0
    out = capsys.readouterr().out
    assert "where to get it:" not in out
    assert "open:" not in out


def test_secret_set_prints_no_hints_without_obtain_or_link(
    scratch_registry: tuple[Home, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _facet_home, registry = scratch_registry
    monkeypatch.setenv("FACET_HOME", str(tmp_path / "facet-home"))
    monkeypatch.setenv("FACET_REGISTRY", str(registry))
    monkeypatch.delenv(secrets.PASSPHRASE_ENV, raising=False)
    _tty_set(monkeypatch)
    assert main(["secret", "set", "poster", "app-password"]) == 0
    out = capsys.readouterr().out
    assert "where to get it:" not in out
    assert "open:" not in out
