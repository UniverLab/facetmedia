"""Renewals side channel: a runner can store a refreshed secret it minted."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from facetmedia import runner, runner_sdk, secrets
from facetmedia.contract import FacetError
from facetmedia.home import Home, log_file
from facetmedia.registry_types import Package, Requirements, Secret

_TEMPLATE = "{{python}} -I {{entry}} {{verb}} --input {{input}} --result {{result}}"


def _package(tmp_path: Path, names: tuple[str, ...] = ("tok",)) -> Package:
    return Package(
        name="pkg",
        version="0.1.0",
        requires_facet=">=0.1.0b1,<0.2",
        protocol=1,
        provider="t",
        tool_name="Pkg",
        description="d",
        facet="text",
        engine_type="python-env",
        python="3.12",
        transport_type="cli",
        entry="runner.py",
        invocation_template=_TEMPLATE,
        requirements=Requirements(),
        directory=tmp_path / "pkg",
        secrets=tuple(Secret(name=name, description=f"{name} secret") for name in names),
    )


def _inv(tmp_path: Path, home: Home, package: Package, run_id: str = "run-1") -> runner.Invocation:
    temp_dir = home.tmp_dir / f".facet-tmp-{run_id}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    return runner.Invocation(
        package=package,
        home=home,
        env_dir=home.env_dir(package.name, package.version),
        temp_dir=temp_dir,
        run_id=run_id,
        timeout_s=None,
        start_server=False,
    )


def _request() -> dict[str, object]:
    return runner.make_request("run", "pkg.cap", {}, Path("/out"), Path("/piece"), {})


def _renewals_path(inv: runner.Invocation, verb: str = "run") -> Path:
    request = json.loads((inv.temp_dir / runner.REQUEST_NAME).read_text(encoding="utf-8"))
    return Path(str(request["renewals_file"]))


def _append(path: Path, name: str, value: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")
        handle.flush()


def test_make_request_carries_renewals_file(tmp_path: Path) -> None:
    request = runner.make_request("run", "pkg.cap", {}, tmp_path, tmp_path, {})
    assert request["renewals_file"] == ""


def test_renew_then_raise_stores_masks_and_cleans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Home.from_env()
    home.ensure()
    package = _package(tmp_path)
    inv = _inv(tmp_path, home, package, run_id="run-a")
    token = "fresh-token-abc123"
    log = log_file(inv.run_id)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(f"before {token} after\n", encoding="utf-8")

    def fake_run_client(
        _inv: runner.Invocation, _verb: str, _argv: Any, tail: Any, _server_url: Any
    ) -> int:
        path = _renewals_path(inv)
        _append(path, "tok", token)
        tail.append(f"printed {token}\n")
        return 1

    monkeypatch.setattr(runner, "_run_client", fake_run_client)
    with pytest.raises(FacetError) as excinfo:
        runner.call(inv, "run", _request())
    err = excinfo.value
    assert err.code == "runner_failed"
    assert token not in err.details.get("output", "")
    assert "***" in err.details.get("output", "")
    assert token not in log.read_text(encoding="utf-8")
    assert secrets.env_for(home, package).get("FACET_SECRET_TOK") == token
    renewals_dir = _renewals_path(inv).parent
    assert not renewals_dir.exists()
    assert not _renewals_path(inv).exists()


def test_double_renew_last_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = Home.from_env()
    home.ensure()
    package = _package(tmp_path)
    inv = _inv(tmp_path, home, package, run_id="run-b")

    def fake_run_client(
        _inv: runner.Invocation, _verb: str, _argv: Any, _tail: Any, _server_url: Any
    ) -> int:
        path = _renewals_path(inv)
        _append(path, "tok", "first")
        _append(path, "tok", "second")
        (inv.temp_dir / runner.RESULT_NAME).write_text("{}", encoding="utf-8")
        return 0

    monkeypatch.setattr(runner, "_run_client", fake_run_client)
    outcome = runner.call(inv, "run", _request())
    assert outcome.warnings == ()
    assert secrets.env_for(home, package).get("FACET_SECRET_TOK") == "second"
    assert not _renewals_path(inv).parent.exists()


def test_undeclared_skipped_declared_stored_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Home.from_env()
    home.ensure()
    package = _package(tmp_path, names=("tok",))
    inv = _inv(tmp_path, home, package, run_id="run-c")

    def fake_run_client(
        _inv: runner.Invocation, _verb: str, _argv: Any, _tail: Any, _server_url: Any
    ) -> int:
        path = _renewals_path(inv)
        _append(path, "nope", "secret-nope")
        _append(path, "tok", "kept")
        (inv.temp_dir / runner.RESULT_NAME).write_text("{}", encoding="utf-8")
        return 0

    monkeypatch.setattr(runner, "_run_client", fake_run_client)
    outcome = runner.call(inv, "run", _request())
    assert secrets.env_for(home, package).get("FACET_SECRET_TOK") == "kept"
    assert "nope" not in secrets.env_for(home, package)
    assert outcome.warnings == ("pkg renewed undeclared secret nope (skipped)",)


def test_undeclared_warning_on_failure_in_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Home.from_env()
    home.ensure()
    package = _package(tmp_path, names=("tok",))
    inv = _inv(tmp_path, home, package, run_id="run-cf")

    def fake_run_client(
        _inv: runner.Invocation, _verb: str, _argv: Any, _tail: Any, _server_url: Any
    ) -> int:
        path = _renewals_path(inv)
        _append(path, "nope", "x")
        _append(path, "tok", "kept")
        return 1

    monkeypatch.setattr(runner, "_run_client", fake_run_client)
    with pytest.raises(FacetError) as excinfo:
        runner.call(inv, "run", _request())
    assert excinfo.value.details.get("warnings") == ["pkg renewed undeclared secret nope (skipped)"]
    assert secrets.env_for(home, package).get("FACET_SECRET_TOK") == "kept"


def test_locked_vault_loses_renewal_cleans_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Home.from_env()
    home.ensure()
    package = _package(tmp_path)
    inv = _inv(tmp_path, home, package, run_id="run-d")
    token = "renewed-but-lost"

    def fake_run_client(
        _inv: runner.Invocation, _verb: str, _argv: Any, tail: Any, _server_url: Any
    ) -> int:
        path = _renewals_path(inv)
        _append(path, "tok", token)
        tail.append(f"leak {token}\n")
        (inv.temp_dir / runner.RESULT_NAME).write_text("{}", encoding="utf-8")
        return 0

    monkeypatch.setattr(runner, "_run_client", fake_run_client)

    def fail_store(*_args: Any, **_kwargs: Any) -> Any:
        raise FacetError("machine_cannot", "vault locked")

    monkeypatch.setattr(secrets, "set_value", fail_store)
    with pytest.raises(FacetError) as excinfo:
        runner.call(inv, "run", _request())
    err = excinfo.value
    assert err.code == "machine_cannot"
    assert err.message == "the renewed secret was lost; run facet auth"
    assert token not in err.details.get("output", "")
    assert not _renewals_path(inv).parent.exists()
    assert not _renewals_path(inv).exists()


def test_renewals_file_created_outside_temp_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Home.from_env()
    home.ensure()
    package = _package(tmp_path)
    inv = _inv(tmp_path, home, package, run_id="run-e")
    seen: dict[str, Path] = {}

    def fake_run_client(
        _inv: runner.Invocation, _verb: str, _argv: Any, _tail: Any, _server_url: Any
    ) -> int:
        path = _renewals_path(inv)
        seen["path"] = path
        assert path.exists()
        assert path.parent.parent == home.tmp_dir
        assert inv.temp_dir not in path.parents
        (inv.temp_dir / runner.RESULT_NAME).write_text("{}", encoding="utf-8")
        return 0

    monkeypatch.setattr(runner, "_run_client", fake_run_client)
    runner.call(inv, "run", _request())
    assert seen["path"].parent.parent == home.tmp_dir
    assert not seen["path"].exists()


def test_renew_secret_appends_flushes_and_survives_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "renewals"
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(runner_sdk, "_current_renewals_file", str(path))
    runner_sdk.renew_secret("tok", "first")
    assert path.read_text(encoding="utf-8") == "tok=first\n"
    try:
        runner_sdk.renew_secret("tok", "second")
        raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert path.read_text(encoding="utf-8") == "tok=first\ntok=second\n"
    monkeypatch.setattr(runner_sdk, "_current_renewals_file", "")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("", "v"),
        ("tok", ""),
        ("bad\nname", "v"),
        ("tok", "bad\nvalue"),
    ],
)
def test_renew_secret_refuses_unsafe_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    path = tmp_path / "renewals"
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(runner_sdk, "_current_renewals_file", str(path))
    with pytest.raises(KeyError):
        runner_sdk.renew_secret(name, value)
    assert path.read_text(encoding="utf-8") == ""
    monkeypatch.setattr(runner_sdk, "_current_renewals_file", "")


def test_renew_secret_requires_current_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_sdk, "_current_renewals_file", "")
    with pytest.raises(KeyError):
        runner_sdk.renew_secret("tok", "value")
