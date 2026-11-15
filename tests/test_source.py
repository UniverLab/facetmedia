"""The pinned source checkout: declared, pulled, exposed, freed, collected.

Some tools ship Python code that is not pip-installable and serves no API.
A package like that declares ``[engine.source]``; ``facet pull`` clones the
pinned rev once per home, the runner finds it in ``FACET_SOURCE_DIR``, and
``facet remove`` and ``facet clean`` free it like a server checkout.
"""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from facetmedia import env as env_module
from facetmedia import prune as prune_module
from facetmedia import runner as runner_module
from facetmedia.home import Home
from facetmedia.registry import load_registry, validate_registry
from facetmedia.registry_types import Package, Source

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry"
REPO_URL = "https://example.invalid/tool.git"
REV = "a" * 40
SHORT_REV = 12

BASE = """name = "pkg"
version = "0.1.0"
requires_facet = ">=0.1.0"
protocol = 1
provider = "test"
tool_name = "Test"
description = "a test package"
facet = "text"

[engine]
type = "python-env"
python = "3.12"

[transport]
type = "cli"
entry = "runner.py"

[requirements]
gpu = false
ram_gb = 1
disk_gb = 1
"""

SOURCE_TABLE = f"""
[engine.source]
repo = "{REPO_URL}"
rev = "{REV}"
"""


def _write_package(root: Path, manifest: str) -> Path:
    """A scratch registry holding one package with this manifest."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.toml").write_text('version = 1\n[[packages]]\nname = "pkg"\n', encoding="utf-8")
    pkg_dir = root / "packages" / "pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "package.toml").write_text(manifest, encoding="utf-8")
    echo = FIXTURES / "packages" / "echo"
    for name in ("pyproject.toml", "uv.lock", "runner.py"):
        shutil.copyfile(echo / name, pkg_dir / name)
    shutil.copytree(echo / "skill", pkg_dir / "skill")
    skill = pkg_dir / "skill" / "SKILL.md"
    skill.write_text(
        skill.read_text(encoding="utf-8").replace("name: echo", "name: pkg"),
        encoding="utf-8",
    )
    return root


def _problems(root: Path) -> set[tuple[str, str]]:
    return {(problem["key"], problem["problem"]) for problem in validate_registry(root)}


def _validate(manifest: str, tmp_path: Path) -> set[tuple[str, str]]:
    root = tmp_path / "registry"
    _write_package(root, manifest)
    return _problems(root)


# --- manifest: the [engine.source] table.


def test_valid_source_loads_and_validates_clean(tmp_path: Path) -> None:
    root = _write_package(tmp_path / "registry", BASE + SOURCE_TABLE)
    assert validate_registry(root) == []
    package = load_registry(root).packages["pkg"]
    assert package.source == Source(repo=REPO_URL, rev=REV)


def test_non_https_repo_is_a_problem(tmp_path: Path) -> None:
    manifest = BASE + SOURCE_TABLE.replace("https://", "http://")
    assert ("engine.source.repo", "value") in _validate(manifest, tmp_path)


def test_short_rev_is_a_problem(tmp_path: Path) -> None:
    manifest = BASE + SOURCE_TABLE.replace(REV, "abc")
    assert ("engine.source.rev", "value") in _validate(manifest, tmp_path)


def test_unknown_source_key_is_a_problem(tmp_path: Path) -> None:
    manifest = BASE + SOURCE_TABLE + 'branch = "main"\n'
    assert ("engine.source.branch", "unknown") in _validate(manifest, tmp_path)


def test_source_requires_python_env_with_cli_transport(tmp_path: Path) -> None:
    manifest = BASE.replace('type = "cli"', 'type = "http"') + SOURCE_TABLE
    problems = _validate(manifest, tmp_path)
    assert ("engine.source", "requires python-env with transport cli") in problems


def test_server_and_source_together_are_a_problem(tmp_path: Path) -> None:
    server_table = f"""
[engine.server]
repo = "{REPO_URL}"
rev = "{REV}"
requirements = "requirements.txt"
launch = ["python", "serve.py", "--port", "{{{{port}}}}"]
health_url = "http://127.0.0.1:{{{{port}}}}/health"
timeout_s = 20
"""
    manifest = BASE + SOURCE_TABLE + server_table
    root = tmp_path / "registry"
    _write_package(root, manifest)
    (root / "packages" / "pkg" / "requirements.txt").write_text("", encoding="utf-8")
    problems = _problems(root)
    assert (
        "engine.source",
        "a package declares either a server or a source checkout",
    ) in problems


# --- pull: a real local git remote, rewritten in process only.


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )


def _make_remote(remote: Path) -> str:
    """A real git repository holding the fixture tool, at one real commit."""
    remote.mkdir(parents=True)
    (remote / "tool.py").write_text("VALUE = 1\n", encoding="utf-8")
    (remote / "README.md").write_text("tool\n", encoding="utf-8")
    _git(remote, "init", "--quiet")
    _git(remote, "add", "-A")
    _git(
        remote,
        "-c",
        "user.name=facet-tests",
        "-c",
        "user.email=facet@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "the fixture tool",
    )
    return _git(remote, "rev-parse", "HEAD").stdout.strip()


def _git_env(remote: Path) -> dict[str, str]:
    """Rewrite the manifest's https URL to the scratch repository, in process only."""
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"url.{remote.as_uri()}.insteadOf",
        "GIT_CONFIG_VALUE_0": REPO_URL,
    }


@dataclass(frozen=True)
class World:
    """The scratch remote, the registry copy and the home all tests share."""

    registry: Path
    remote: Path
    rev: str
    home: Path
    git_env: dict[str, str]


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> World:
    root = tmp_path_factory.mktemp("source-world")
    remote = root / "remote"
    rev = _make_remote(remote)
    registry = root / "registry"
    shutil.copytree(FIXTURES, registry)
    return World(
        registry=registry,
        remote=remote,
        rev=rev,
        home=root / "facet-home",
        git_env=_git_env(remote),
    )


def _source_package(world: World, name: str = "echo") -> Package:
    return dataclasses.replace(
        load_registry(world.registry).packages[name],
        source=Source(repo=REPO_URL, rev=world.rev),
    )


@pytest.fixture
def source_home(world: World, monkeypatch: pytest.MonkeyPatch, _facet_home_tmp: None) -> Home:
    """A home pointed at the scratch world, with git rewriting in process."""
    _ = _facet_home_tmp
    monkeypatch.setenv("FACET_HOME", str(world.home))
    monkeypatch.setenv("FACET_REGISTRY", str(world.registry))
    for name, value in world.git_env.items():
        monkeypatch.setenv(name, value)
    return Home.from_env()


def test_fetch_source_clones_once_and_marks_the_rev(source_home: Home, world: World) -> None:
    package = _source_package(world)
    checkout = env_module.source_dir(package, source_home)

    env_module.fetch_source(package, source_home)

    assert checkout.name == world.rev[:SHORT_REV]
    assert (checkout / "tool.py").is_file()
    assert (checkout / ".complete").read_text(encoding="utf-8").strip() == world.rev
    assert env_module.source_present(package, source_home) is True


def test_fetch_source_skips_a_verified_checkout(source_home: Home, world: World) -> None:
    package = _source_package(world)
    env_module.fetch_source(package, source_home)
    checkout = env_module.source_dir(package, source_home)
    sentinel = checkout / "sentinel"
    sentinel.write_text("left by the first fetch", encoding="utf-8")

    env_module.fetch_source(package, source_home)

    assert sentinel.is_file()


def test_fetch_source_is_redone_when_the_marker_is_missing(source_home: Home, world: World) -> None:
    package = _source_package(world)
    env_module.fetch_source(package, source_home)
    checkout = env_module.source_dir(package, source_home)
    (checkout / ".complete").unlink()
    assert env_module.source_present(package, source_home) is False

    env_module.fetch_source(package, source_home)

    assert (checkout / ".complete").read_text(encoding="utf-8").strip() == world.rev
    assert env_module.source_present(package, source_home) is True


def test_pull_twice_reports_already_installed_the_second_time(
    capsys: pytest.CaptureFixture[str], source_home: Home, world: World
) -> None:
    package = _source_package(world)

    first = env_module.pull(package, source_home)
    second = env_module.pull(package, source_home)

    assert first["already_installed"] is False
    assert second["already_installed"] is True
    assert env_module.is_installed(package, source_home) is True
    capsys.readouterr()


# --- runner env: the checkout path the runner puts on sys.path.


def test_runner_env_sets_facet_source_dir(source_home: Home, world: World) -> None:
    package = _source_package(world)
    env_vars = runner_module.runner_env(source_home, package)
    assert env_vars["FACET_SOURCE_DIR"] == str(env_module.source_dir(package, source_home))


def test_runner_env_omits_facet_source_dir_without_a_source(
    source_home: Home, world: World
) -> None:
    plain = load_registry(world.registry).packages["echo"]
    assert "FACET_SOURCE_DIR" not in runner_module.runner_env(source_home, plain)


# --- remove and garbage collection: the checkout tree is freed like a server's.


def test_remove_frees_the_source_checkout(source_home: Home, world: World) -> None:
    package = _source_package(world)
    env_module.fetch_source(package, source_home)
    checkout = env_module.source_dir(package, source_home)
    assert checkout.is_dir()

    result = prune_module.rm_package(source_home, package.name)

    assert checkout.exists() is False
    removed = [str(Path(path)) for path in result["removed"]]
    assert str(checkout.parent.parent) in removed


def test_gc_keeps_a_complete_current_source_checkout(source_home: Home, world: World) -> None:
    package = _source_package(world)
    env_module.fetch_source(package, source_home)
    checkout = env_module.source_dir(package, source_home)

    reason = prune_module._server_reason(source_home, package, checkout.parent)

    assert reason is None


def test_gc_flags_a_stale_source_version(source_home: Home, world: World) -> None:
    package = _source_package(world)
    env_module.fetch_source(package, source_home)
    stale = env_module.source_dir(package, source_home).parent / "0.0.9"
    stale.mkdir(parents=True)

    reason = prune_module._server_reason(source_home, package, stale)

    assert reason == prune_module.STALE_VERSION


def test_gc_flags_a_source_checkout_without_its_marker(source_home: Home, world: World) -> None:
    package = _source_package(world)
    env_module.fetch_source(package, source_home)
    checkout = env_module.source_dir(package, source_home)
    (checkout / ".complete").unlink()

    reason = prune_module._server_reason(source_home, package, checkout.parent)

    assert reason == prune_module.INCOMPLETE_SERVER


def test_pull_plan_announces_the_source_checkout(
    capsys: pytest.CaptureFixture[str], source_home: Home, world: World
) -> None:
    package = _source_package(world)
    env_dir = source_home.env_dir(package.name, package.version)

    env_module._print_plan(package, source_home, env_dir, False)

    err = capsys.readouterr().err
    assert f"source {REPO_URL} @ {world.rev[:SHORT_REV]}" in err
