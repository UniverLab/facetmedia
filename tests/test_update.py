"""``facet update`` and the registry snapshot it pins.

Every GitHub call is served by ``tests/registry_server.FakeGitHub``; nothing
here touches the network.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import urllib.error
from email.message import Message
from pathlib import Path

import pytest

from facetmedia import __version__, lock, update
from facetmedia.cli import main
from facetmedia.contract import FacetError
from facetmedia.home import Home
from facetmedia.registry import load_active_registry, registry_dir
from tests.registry_server import (
    COMMIT,
    FIXTURE_ROOT,
    OTHER_COMMIT,
    FakeGitHub,
    archive_with_device,
    archive_with_file_top,
    archive_with_member,
    archive_with_symlink,
    archive_without_top,
    build_archive,
    dead_urls,
)

SOURCE = "https://github.com/UniverLab/facetmedia-registry"
FIXTURE_PACKAGES = ["echo", "future", "termsy"]
#: update_registry makes exactly two GitHub calls: the commit, then the archive.
GITHUB_CALLS = 2
EXIT_DOWNLOAD_FAILED = 8
#: X-RateLimit-Reset epoch 1766000000, formatted the way facet formats it.
RESET_ISO = "2025-12-17T19:33:20Z"
NO_TOKEN_HINT = "set GITHUB_TOKEN to a GitHub token to lift the limit, or retry after the reset"


def _home() -> Path:
    return Path(os.environ["FACET_HOME"])


def _state() -> dict[str, object]:
    text = (_home() / "state" / "registry.json").read_text(encoding="utf-8")
    state: dict[str, object] = json.loads(text)
    return state


def _registry_dirs() -> list[str]:
    root = _home() / "registry"
    return sorted(path.name for path in root.iterdir()) if root.is_dir() else []


def _changed(data: dict[str, object]) -> dict[str, bool]:
    packages = data["packages"]
    assert isinstance(packages, list)
    return {str(package["name"]): bool(package["changed"]) for package in packages}


def _broken_registry(tmp_path: Path) -> Path:
    """A checkout whose index names a package whose manifest is invalid."""
    root = tmp_path / "broken-registry"
    (root / "packages" / "broken").mkdir(parents=True)
    (root / "index.toml").write_text(
        'version = 1\n[[packages]]\nname = "broken"\n', encoding="utf-8"
    )
    (root / "packages" / "broken" / "package.toml").write_text(
        'name = "wrong"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    return root


def _bumped_registry(tmp_path: Path) -> Path:
    """The fixture registry with echo's manifest version raised."""
    root = tmp_path / "bumped-registry"
    shutil.copytree(FIXTURE_ROOT, root)
    manifest = root / "packages" / "echo" / "package.toml"
    text = manifest.read_text(encoding="utf-8").replace('version = "0.1.0"', 'version = "0.2.0"', 1)
    manifest.write_text(text, encoding="utf-8")
    return root


def test_update_creates_the_snapshot_and_state(fake_github: FakeGitHub) -> None:
    data = update.update_registry(Home.from_env(), "main")

    snapshot = _home() / "registry" / COMMIT
    assert (snapshot / ".complete").is_file()
    assert (snapshot / "index.toml").is_file()
    assert (snapshot / "packages" / "echo" / "package.toml").is_file()
    assert _registry_dirs() == [COMMIT]
    assert list((_home() / "tmp").iterdir()) == [], "the tarball is not left in tmp"

    state = _state()
    assert state["commit"] == COMMIT
    assert state["ref"] == "main"
    assert state["source"] == SOURCE
    assert str(state["fetched_at"]).endswith("Z")

    assert data["commit"] == COMMIT
    assert data["ref"] == "main"
    assert data["previous_commit"] is None
    assert [package["name"] for package in data["packages"]] == FIXTURE_PACKAGES
    assert _changed(data) == {"echo": True, "future": True, "termsy": True}
    assert registry_dir() == snapshot


def test_package_records_carry_the_manifest_fields(fake_github: FakeGitHub) -> None:
    records = update.update_registry(Home.from_env(), "main")["packages"]
    by_name = {str(record["name"]): record for record in records}
    assert by_name["echo"] == {
        "name": "echo",
        "version": "0.1.0",
        "compatible": True,
        "changed": True,
    }
    assert by_name["future"]["compatible"] is False


def test_second_update_to_the_same_commit_only_switches(fake_github: FakeGitHub) -> None:
    home = Home.from_env()
    update.update_registry(home, "main")
    assert fake_github.archive_hits == 1

    data = update.update_registry(home, "feature/v0.1")

    assert fake_github.archive_hits == 1
    assert fake_github.api_hits == GITHUB_CALLS
    assert data["commit"] == COMMIT
    assert data["previous_commit"] == COMMIT
    assert _changed(data) == {"echo": False, "future": False, "termsy": False}
    assert _state()["ref"] == "feature/v0.1"


def test_archive_with_parent_member_is_refused(fake_github: FakeGitHub) -> None:
    fake_github.archive = archive_with_member("top/../evil")

    with pytest.raises(FacetError) as exc:
        update.update_registry(Home.from_env(), "main")

    assert exc.value.code == "download_failed"
    assert "evil" in exc.value.message
    assert _registry_dirs() == []
    assert not (_home() / "state" / "registry.json").exists()
    assert list((_home() / "tmp").iterdir()) == []


def test_archive_with_symlink_member_is_refused(fake_github: FakeGitHub) -> None:
    fake_github.archive = archive_with_symlink()

    with pytest.raises(FacetError) as exc:
        update.update_registry(Home.from_env(), "main")

    assert exc.value.code == "download_failed"
    assert "link" in exc.value.message
    assert _registry_dirs() == []


def test_archive_with_absolute_member_is_refused(fake_github: FakeGitHub) -> None:
    fake_github.archive = archive_with_member("/etc/evil")

    with pytest.raises(FacetError) as exc:
        update.update_registry(Home.from_env(), "main")

    assert exc.value.code == "download_failed"
    assert "/etc/evil" in exc.value.message
    assert _registry_dirs() == []


def test_archive_with_a_device_member_is_refused(fake_github: FakeGitHub) -> None:
    fake_github.archive = archive_with_device()

    with pytest.raises(FacetError) as exc:
        update.update_registry(Home.from_env(), "main")

    assert exc.value.code == "download_failed"
    assert "device" in exc.value.message
    assert _registry_dirs() == []


def test_archive_whose_manifests_do_not_validate_is_refused(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    fake_github.archive = build_archive(_broken_registry(tmp_path))

    with pytest.raises(FacetError) as exc:
        update.update_registry(Home.from_env(), "main")

    assert exc.value.code == "download_failed"
    assert "does not validate" in exc.value.message
    assert _registry_dirs() == []
    assert not (_home() / "state" / "registry.json").exists()
    assert list((_home() / "tmp").iterdir()) == []


def test_corrupt_archive_is_refused(fake_github: FakeGitHub) -> None:
    fake_github.archive = b"not a tarball"

    with pytest.raises(FacetError) as exc:
        update.update_registry(Home.from_env(), "main")

    assert exc.value.code == "download_failed"
    assert _registry_dirs() == []


def test_archive_without_a_single_top_directory_is_refused(fake_github: FakeGitHub) -> None:
    fake_github.archive = archive_without_top()

    with pytest.raises(FacetError) as exc:
        update.update_registry(Home.from_env(), "main")

    assert exc.value.code == "download_failed"
    assert "single top directory" in exc.value.message
    assert _registry_dirs() == []


def test_archive_whose_top_is_a_file_is_refused(fake_github: FakeGitHub) -> None:
    fake_github.archive = archive_with_file_top()

    with pytest.raises(FacetError) as exc:
        update.update_registry(Home.from_env(), "main")

    assert exc.value.code == "download_failed"
    assert _registry_dirs() == []


def test_api_answer_that_is_not_a_commit_is_refused(fake_github: FakeGitHub) -> None:
    fake_github.commit = "not-a-commit"

    with pytest.raises(FacetError) as exc:
        update.update_registry(Home.from_env(), "main")

    assert exc.value.code == "download_failed"
    assert "no commit" in exc.value.message
    assert _registry_dirs() == []


def test_corrupt_state_is_ignored_by_update(fake_github: FakeGitHub) -> None:
    state_file = _home() / "state" / "registry.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text("{not json", encoding="utf-8")

    data = update.update_registry(Home.from_env(), "main")

    assert data["previous_commit"] is None
    assert _state()["commit"] == COMMIT


def test_missing_previous_snapshot_marks_every_package_changed(fake_github: FakeGitHub) -> None:
    state_file = _home() / "state" / "registry.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"commit": OTHER_COMMIT}), encoding="utf-8")

    data = update.update_registry(Home.from_env(), "main")

    assert data["previous_commit"] == OTHER_COMMIT
    assert _changed(data) == {"echo": True, "future": True, "termsy": True}


def test_offline_after_one_update_keeps_working(
    fake_github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    update.update_registry(Home.from_env(), "main")
    dead_urls(monkeypatch)

    assert registry_dir() == _home() / "registry" / COMMIT
    assert set(load_active_registry().packages) == set(FIXTURE_PACKAGES)

    with pytest.raises(FacetError) as exc:
        update.update_registry(Home.from_env(), "main")
    assert exc.value.code == "download_failed"
    assert registry_dir() == _home() / "registry" / COMMIT


def test_failed_update_keeps_the_previous_snapshot(fake_github: FakeGitHub) -> None:
    update.update_registry(Home.from_env(), "main")
    before = (_home() / "state" / "registry.json").read_text(encoding="utf-8")
    fake_github.commit = OTHER_COMMIT

    with pytest.raises(FacetError) as exc:
        update.update_registry(Home.from_env(), "main")

    assert exc.value.code == "download_failed"
    assert (_home() / "state" / "registry.json").read_text(encoding="utf-8") == before
    assert _registry_dirs() == [COMMIT]
    assert registry_dir() == _home() / "registry" / COMMIT


def test_a_version_bump_is_reported_as_changed(fake_github: FakeGitHub, tmp_path: Path) -> None:
    home = Home.from_env()
    update.update_registry(home, "main")
    fake_github.commit = OTHER_COMMIT
    fake_github.archive_commit = OTHER_COMMIT
    fake_github.archive = build_archive(
        _bumped_registry(tmp_path), top=f"registry-{OTHER_COMMIT[:7]}"
    )

    data = update.update_registry(home, "main")

    assert data["previous_commit"] == COMMIT
    assert _changed(data) == {"echo": True, "future": False, "termsy": False}
    assert _registry_dirs() == sorted([COMMIT, OTHER_COMMIT])


def test_first_use_fetches_the_registry(
    fake_github: FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    loaded = load_active_registry()

    assert set(loaded.packages) == set(FIXTURE_PACKAGES)
    assert "fetching the registry (first use)\n" in capsys.readouterr().err
    assert fake_github.archive_hits == 1
    assert _state()["commit"] == COMMIT


def test_first_use_offline_fails_with_the_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dead_urls(monkeypatch)
    monkeypatch.delenv("FACET_REGISTRY", raising=False)

    with pytest.raises(FacetError) as exc:
        registry_dir()

    assert exc.value.code == "download_failed"
    assert exc.value.hint == "run facet update when online"
    assert "fetching the registry (first use)\n" in capsys.readouterr().err


def test_local_registry_override_wins(
    fake_github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FACET_REGISTRY", str(FIXTURE_ROOT))

    assert registry_dir() == FIXTURE_ROOT
    assert set(load_active_registry().packages) == set(FIXTURE_PACKAGES)
    assert fake_github.api_hits == 0
    assert fake_github.archive_hits == 0


def test_default_ref_follows_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FACET_REGISTRY_REF", raising=False)
    assert update.default_ref() == "main"
    monkeypatch.setenv("FACET_REGISTRY_REF", "feature/v0.1")
    assert update.default_ref() == "feature/v0.1"


def test_cli_update_uses_the_ref_flag_quietly(
    fake_github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FACET_REGISTRY_REF", "main")

    assert main(["update", "--ref", "feature/v0.1", "--json"]) == 0

    assert _state()["ref"] == "feature/v0.1"
    assert fake_github.api_hits == 1


def test_cli_update_uses_the_environment_ref(
    fake_github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FACET_REGISTRY_REF", "feature/v0.1")

    assert main(["update", "--json"]) == 0

    assert _state()["ref"] == "feature/v0.1"


def test_cli_update_holds_the_exclusive_lock(
    fake_github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = update.update_registry

    def _while_locked(home: Home, ref: str | None = None) -> dict[str, object]:
        with (  # a second descriptor on the same lock file must be refused
            pytest.raises(FacetError) as blocked,
            lock.hold("exclusive", "probe", wait=False, timeout_s=0.0),
        ):
            pass
        assert blocked.value.code == "lock_busy", "facet update ran without the lock"
        return real(home, ref)

    monkeypatch.setattr(update, "update_registry", _while_locked)

    assert main(["update", "--json"]) == 0
    assert fake_github.archive_hits == 1


def test_cli_update_json_envelope(
    fake_github: FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["update", "--ref", "feature/v0.1", "--json"]) == 0

    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert envelope["ok"] is True
    assert envelope["data"]["commit"] == COMMIT
    assert envelope["data"]["ref"] == "feature/v0.1"
    assert [package["name"] for package in envelope["data"]["packages"]] == FIXTURE_PACKAGES


def test_cli_update_human_output(
    fake_github: FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["update", "--ref", "feature/v0.1"]) == 0

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert lines[0] == f"registry {COMMIT} (ref feature/v0.1)"
    assert lines[1:] == [
        "  echo 0.1.0 changed",
        "  future 0.1.0 changed",
        "  termsy 0.1.0 changed",
    ]
    assert captured.err == ""


def test_cli_update_failure_is_download_failed(
    fake_github: FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_github.commit = OTHER_COMMIT

    assert main(["update", "--json"]) == EXIT_DOWNLOAD_FAILED

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "download_failed"
    assert envelope["error"]["hint"] == "run facet update when online"


def test_requests_carry_the_user_agent(fake_github: FakeGitHub) -> None:
    update.update_registry(Home.from_env(), "main")

    assert len(fake_github.requests) == GITHUB_CALLS
    for _path, headers in fake_github.requests:
        assert headers["user-agent"] == f"facetmedia/{__version__}"
        assert "authorization" not in headers


def test_requests_send_the_token_when_set(
    fake_github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")

    update.update_registry(Home.from_env(), "main")

    assert len(fake_github.requests) == GITHUB_CALLS
    for _path, headers in fake_github.requests:
        assert headers["authorization"] == "Bearer secret-token"


def test_api_answers_a_sha_and_the_archive_follows_that_commit(
    fake_github: FakeGitHub,
) -> None:
    assert update.resolve_ref("feature/v0.1") == COMMIT
    assert fake_github.requests[0][0] == (
        "/repos/UniverLab/facetmedia-registry/commits/feature/v0.1"
    )
    assert fake_github.requests[0][1]["accept"] == "application/vnd.github.sha"

    home = Home.from_env()
    archive = update.download_archive(home, COMMIT)
    assert archive == home.tmp_dir / f"registry-{COMMIT}.tar.gz"
    assert archive.is_file()
    assert fake_github.archive_hits == 1


def test_unknown_commit_archive_is_a_download_failure(fake_github: FakeGitHub) -> None:
    home = Home.from_env()
    with pytest.raises(FacetError) as exc:
        update.download_archive(home, OTHER_COMMIT)
    assert exc.value.code == "download_failed"
    assert "HTTP 404" in exc.value.message
    assert list(home.tmp_dir.iterdir()) == []


def test_a_cut_connection_is_a_download_failure(
    fake_github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _cut(*_args: object, **_kwargs: object) -> None:
        raise http.client.IncompleteRead(b"partial", 4096)

    monkeypatch.setattr("urllib.request.urlopen", _cut)
    home = Home.from_env()

    with pytest.raises(FacetError) as exc:
        update.download_archive(home, COMMIT)

    assert exc.value.code == "download_failed"
    assert exc.value.hint == "run facet update when online"
    assert list(home.tmp_dir.iterdir()) == []


def _http_error(headers: Message | None) -> urllib.error.HTTPError:
    """A GitHub 403 carrying ``headers``, built without any network."""
    return urllib.error.HTTPError(
        "https://api.github.com/repos/UniverLab/facetmedia-registry/commits/main",
        403,
        "Forbidden",
        headers,
        None,
    )


def _rate_limited() -> urllib.error.HTTPError:
    """A GitHub 403 that has exhausted the anonymous quota, resetting at the epoch."""
    headers = Message()
    headers["X-RateLimit-Remaining"] = "0"
    headers["X-RateLimit-Reset"] = "1766000000"
    return _http_error(headers)


def _fails_with(error: urllib.error.HTTPError) -> object:
    def _raise(*_args: object, **_kwargs: object) -> None:
        raise error

    return _raise


def test_rate_limited_403_reports_the_reset_and_the_token_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr("urllib.request.urlopen", _fails_with(_rate_limited()))

    with pytest.raises(FacetError) as exc:
        update.resolve_ref("main")

    assert exc.value.code == "download_failed"
    assert "GitHub rate limit reached" in exc.value.message
    assert RESET_ISO in exc.value.message
    assert exc.value.hint == NO_TOKEN_HINT
    assert exc.value.details["rate_limited"] is True
    assert exc.value.details["reset"] == RESET_ISO


def test_rate_limited_403_with_a_token_only_suggests_the_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
    monkeypatch.setattr("urllib.request.urlopen", _fails_with(_rate_limited()))

    with pytest.raises(FacetError) as exc:
        update.resolve_ref("main")

    assert exc.value.code == "download_failed"
    assert "GitHub rate limit reached" in exc.value.message
    assert exc.value.hint == "retry after the reset"
    assert exc.value.details["rate_limited"] is True


def test_plain_403_stays_the_plain_http_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    for headers in (Message(), None):  # no rate-limit header at all, and no headers object
        monkeypatch.setattr("urllib.request.urlopen", _fails_with(_http_error(headers)))

        with pytest.raises(FacetError) as exc:
            update.resolve_ref("main")

        assert exc.value.code == "download_failed"
        assert "HTTP 403" in exc.value.message
        assert "rate_limited" not in exc.value.details
        assert exc.value.hint == "run facet update when online"
