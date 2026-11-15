"""The ``host-app`` engine: locate, version-check, drive by script, never install.

No Blender or FreeCAD is ever present: each test builds a stub binary in the
application's argv shape (a Python script, so it prints ``--version`` and runs
the package script), and the version the stub prints decides the manifest's
minimum.  The registry is a copy of the shared fixtures with one host-app
package added.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from facetmedia import config as config_module
from facetmedia import doctor as doctor_module
from facetmedia import env as env_module
from facetmedia import runner as runner_module
from facetmedia.cli import main
from facetmedia.home import Home
from facetmedia.registry import load_registry, validate_registry
from facetmedia.registry_types import HostApp, Package

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry"
PACKAGE = "hostblend"
VERSION = "4.2.0"
ENTRY = "render.py"
FREE_ENTRY = "draw.py"
EXIT_INVALID_INPUT = 2
EXIT_MACHINE_CANNOT = 5
EXIT_RUNNER_FAILED = 9
SCRIPT_EXIT = 7

ENTRY_SCRIPT = '''"""The fixture host script: writes the run's result file."""

import json
import sys
from pathlib import Path

args = sys.argv[1:]
request = json.loads(Path(args[args.index("--input") + 1]).read_text())
result = Path(args[args.index("--result") + 1])
out_dir = Path(request["out_dir"])
(out_dir / "host-out.txt").write_text("rendered", encoding="utf-8")
result.write_text(
    json.dumps(
        {
            "tool": {"name": "hostblend", "version": "0.1.0"},
            "assets": [
                {
                    "file": "host-out.txt",
                    "name": "host-out",
                    "kind": "cad",
                    "mime": "text/plain",
                    "properties": {},
                    "params": {},
                    "seed": None,
                    "inputs": [],
                }
            ],
        }
    )
)
'''

FAILING_SCRIPT = "raise SystemExit(7)\n"

BLENDER_STUB = '''#!{python}
"""A stub Blender: version probe, argv record, script execution."""

import json
import subprocess
import sys
from pathlib import Path

RECORD = Path(@@RECORD@@)


def main() -> int:
    args = sys.argv[1:]
    if "--version" in args:
        print("Blender {version}")
        return 0
    RECORD.write_text(json.dumps(sys.argv), encoding="utf-8")
    script = args[args.index("--python") + 1]
    rest = args[args.index("--python") + 2 :]
    return subprocess.call([sys.executable, script, *rest])


raise SystemExit(main())
'''

FREECAD_STUB = '''#!{python}
"""A stub freecadcmd: version probe, then the wrapper runs the script."""

import runpy
import sys


def main() -> int:
    args = sys.argv[1:]
    if "--version" in args:
        print("FreeCAD {version}")
        return 0
    sys.argv = [args[0], *args[1:]]
    runpy.run_path(args[0], run_name="__main__")
    return 0


raise SystemExit(main())
'''

MANIFEST = """name = "hostblend"
version = "0.1.0"
requires_facet = ">=0.1.0b1,<0.2"
protocol = 1
provider = "facet"
tool_name = "HostBlend"
description = "A host-app engine fixture package."
facet = "cad"

[engine]
type = "host-app"

[engine.host_app]
app = "@@APP@@"
min_version = "@@MINIMUM@@"
@@EXTRA@@

[transport]
type = "cli"
entry = "@@ENTRY@@"
invocation_template = "{{app}} {{entry}} --input {{input}} --result {{result}}"

[requirements]
gpu = false
ram_gb = 0.1
disk_gb = 0.01
exclusive = false
platforms = ["linux"]
host_libs = []
host_hint = ""

[capabilities.render]
summary = "Render through the host application stub."
kind = "cad"

[capabilities.render.preview]
viewer = "generic"
"""

SKILL = """---
name: hostblend
description: A host-app fixture skill for locate, pin and run tests.
---
# Hostblend

Body mentions hostblend.render.

## When to use

Use it in tests.

## Pieces

```toml
[inputs]
```

Pieces for hostblend.render.

## Judging the result

Verify passes; read the generic preview fields.

## Pitfalls

- None.
"""


@dataclass(frozen=True)
class World:
    """The registry copy and the stub binary the manifest expects."""

    registry: Path
    binary: Path
    app: str
    env_var: str

    def package(self) -> Package:
        return load_registry(self.registry).packages[PACKAGE]

    def app_table(self) -> HostApp:
        hosted = self.package().host_app
        assert hosted is not None
        return hosted


def _write_binary(path: Path, template: str, app_version: str, record: Path | None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = template.replace("{python}", sys.executable).replace("{version}", app_version)
    literal = json.dumps(str(record)) if record is not None else '"unused-argv.json"'
    text = text.replace("@@RECORD@@", literal)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def _world(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    app: str = "blender",
    app_version: str = VERSION,
    minimum: str = "4.0",
    extra: str = "",
) -> World:
    registry = tmp_path / "registry"
    shutil.copytree(FIXTURES, registry)
    package_dir = registry / "packages" / PACKAGE
    package_dir.mkdir()
    entry = ENTRY if app == "blender" else FREE_ENTRY
    manifest = (
        MANIFEST.replace("@@APP@@", app)
        .replace("@@MINIMUM@@", minimum)
        .replace("@@EXTRA@@", extra)
        .replace("@@ENTRY@@", entry)
    )
    (package_dir / "package.toml").write_text(manifest, encoding="utf-8")
    (package_dir / entry).write_text(ENTRY_SCRIPT, encoding="utf-8")
    skill_dir = package_dir / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(SKILL, encoding="utf-8")
    index = registry / "index.toml"
    index.write_text(
        index.read_text(encoding="utf-8") + f'\n[[packages]]\nname = "{PACKAGE}"\n',
        encoding="utf-8",
    )
    env_var = "BLENDER_EXE" if app == "blender" else "FREECAD_EXE"
    template = BLENDER_STUB if app == "blender" else FREECAD_STUB
    record = tmp_path / "argv.json" if app == "blender" else None
    binary = _write_binary(
        tmp_path / "bin" / ("blender" if app == "blender" else "freecadcmd"),
        template,
        app_version,
        record,
    )
    monkeypatch.setenv("FACET_REGISTRY", str(registry))
    monkeypatch.delenv(env_var, raising=False)
    return World(registry, binary, app, env_var)


def _problems(world: World) -> set[tuple[str, str, str]]:
    return {
        (problem["file"], problem["key"], problem["problem"])
        for problem in validate_registry(world.registry)
    }


def _manifest(world: World) -> Path:
    return world.registry / "packages" / PACKAGE / "package.toml"


def _rewrite(world: World, old: str, new: str) -> None:
    path = _manifest(world)
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


def _json(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, object]]:
    code = main([*argv, "--json"])
    envelope = json.loads(capsys.readouterr().out)
    assert isinstance(envelope, dict)
    return code, envelope


def _error(envelope: dict[str, object]) -> dict[str, object]:
    error = envelope.get("error")
    assert isinstance(error, dict)
    return error


# --- validate: the host-app declaration and its rejections.


def test_validate_parses_the_table_and_the_lab_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch)

    assert _problems(world) == set()
    package = world.package()
    assert package.engine_type == "host-app"
    assert package.python == ""
    assert package.host_app == HostApp(
        app="blender", min_version="4.0", config_key="blender.path", env="BLENDER_EXE"
    )
    assert main(["registry", "validate", str(world.registry)]) == 0


def test_validate_accepts_a_two_component_minimum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch, app="freecad", app_version="1.1.0", minimum="1.1")

    assert _problems(world) == set()
    assert world.app_table().min_version == "1.1"
    assert world.app_table().env == "FREECAD_EXE"


def test_validate_rejects_an_unknown_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _world(tmp_path, monkeypatch)

    _rewrite(world, 'app = "blender"', 'app = "gimp"')

    assert ("packages/hostblend/package.toml", "engine.host_app.app", "value") in _problems(world)


def test_validate_rejects_a_non_numeric_minimum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch)

    _rewrite(world, 'min_version = "4.0"', 'min_version = "latest"')

    assert (
        "packages/hostblend/package.toml",
        "engine.host_app.min_version",
        "value",
    ) in _problems(world)


def test_validate_rejects_engine_python_on_a_host_app_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch)

    _rewrite(world, 'type = "host-app"', 'type = "host-app"\npython = "3.12"')

    assert ("packages/hostblend/package.toml", "engine.python", "value") in _problems(world)


# --- config: the known-key universe, round trip, and rejection.


def test_config_round_trips_on_a_scratch_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path, monkeypatch)
    home = Home.from_env()

    code, envelope = _json(capsys, "config", "set", "blender.path", str(world.binary))
    assert code == 0
    assert envelope["data"]["value"] == str(world.binary)
    text = config_module.config_file(home).read_text(encoding="utf-8")
    assert f'blender.path = "{world.binary}"' in text

    code, envelope = _json(capsys, "config", "get", "blender.path")
    assert code == 0
    assert envelope["data"] == {
        "key": "blender.path",
        "value": str(world.binary),
        "set": True,
        "path": str(config_module.config_file(home)),
    }

    code, envelope = _json(capsys, "config", "list")
    assert code == 0
    assert envelope["data"]["keys"] == [
        {"key": "blender.path", "app": "blender", "value": str(world.binary), "set": True}
    ]


def test_config_rejects_an_unknown_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _world(tmp_path, monkeypatch)

    code, envelope = _json(capsys, "config", "set", "gimp.path", "/usr/bin/gimp")

    assert code == EXIT_INVALID_INPUT
    error = _error(envelope)
    assert error["code"] == "invalid_input"
    assert error["details"]["known"] == ["blender.path"]


# --- resolution order: config over env over PATH.


def test_resolution_order_is_config_then_env_then_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch)
    home = Home.from_env()
    from_config = _write_binary(tmp_path / "a" / "blender", BLENDER_STUB, VERSION, None)
    from_env = _write_binary(tmp_path / "b" / "blender", BLENDER_STUB, VERSION, None)
    from_path = _write_binary(tmp_path / "c" / "blender", BLENDER_STUB, VERSION, None)
    config_module.set_value(home, load_registry(world.registry), "blender.path", str(from_config))
    monkeypatch.setenv(world.env_var, str(from_env))
    monkeypatch.setenv("PATH", str(from_path.parent))

    assert config_module.resolve(home, world.app_table()) == config_module.HostAppPath(
        str(from_config), "config blender.path"
    )

    config_module.config_file(home).write_text("", encoding="utf-8")
    assert config_module.resolve(home, world.app_table()) == config_module.HostAppPath(
        str(from_env), "env BLENDER_EXE"
    )

    monkeypatch.delenv(world.env_var)
    assert config_module.resolve(home, world.app_table()) == config_module.HostAppPath(
        str(from_path), "PATH"
    )


# --- pull: nothing downloaded, the two exact machine_cannot messages.


def test_pull_reports_a_missing_app_with_every_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _world(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    code, envelope = _json(capsys, "pull", PACKAGE)

    assert code == EXIT_MACHINE_CANNOT
    error = _error(envelope)
    assert error["code"] == "machine_cannot"
    assert error["message"] == (
        "blender 4.0+ not found (config blender.path, env BLENDER_EXE, PATH all empty); "
        "fix: facet config set blender.path /path/to/blender"
    )


def test_pull_reports_a_below_minimum_app_with_the_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path, monkeypatch, app_version="3.1.0")
    config_module.set_value(
        Home.from_env(), load_registry(world.registry), "blender.path", str(world.binary)
    )

    code, envelope = _json(capsys, "pull", PACKAGE)

    assert code == EXIT_MACHINE_CANNOT
    error = _error(envelope)
    assert error["message"] == (
        "blender 3.1.0 found, needs 4.0+ (config blender.path); "
        "fix: facet config set blender.path /path/to/blender"
    )


def test_pull_downloads_nothing_and_is_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path, monkeypatch)
    home = Home.from_env()
    config_module.set_value(home, load_registry(world.registry), "blender.path", str(world.binary))

    code, envelope = _json(capsys, "pull", PACKAGE)

    assert code == 0
    data = envelope["data"]
    assert data["already_installed"] is True
    assert data["downloaded_bytes"] == 0
    assert env_module.is_installed(world.package(), home) is True
    assert not (home.env_dir(PACKAGE, "0.1.0") / ".complete").exists()
    entry = json.loads(home.state_file("installed.json").read_text(encoding="utf-8"))
    assert entry[PACKAGE]["version"] == "0.1.0"
    assert entry[PACKAGE]["registry_commit"] == ""

    code, _envelope = _json(capsys, "remove", PACKAGE)

    assert code == 0
    assert PACKAGE not in json.loads(home.state_file("installed.json").read_text(encoding="utf-8"))


# --- the runner: injected flags, their order, and failing scripts.


def test_blender_injects_the_exit_code_flag_before_the_script_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch)
    home = Home.from_env()
    package = world.package()
    config_module.set_value(home, load_registry(world.registry), "blender.path", str(world.binary))
    entry = str(package.directory / ENTRY)

    argv = runner_module.build_argv(
        package, home, home.env_dir(PACKAGE, "0.1.0"), "run", Path("/in"), Path("/out")
    )

    assert argv[:7] == [
        str(world.binary),
        "-b",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        entry,
    ]
    assert argv.index("--python-exit-code") < argv.index("--python")
    assert argv[7:] == ["--input", "/in", "--result", "/out"]


def test_run_uses_the_injected_flags_and_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path, monkeypatch)
    home = Home.from_env()
    config_module.set_value(home, load_registry(world.registry), "blender.path", str(world.binary))
    piece = tmp_path / "piece.toml"
    piece.write_text("[inputs]\n", encoding="utf-8")
    out = tmp_path / "out"

    code = main(
        [
            "run",
            f"{PACKAGE}.render",
            str(piece),
            "--out",
            str(out),
            "--no-verify",
            "--json",
        ]
    )
    envelope = json.loads(capsys.readouterr().out)

    assert code == 0, envelope
    assert (out / "host-out.txt").read_text(encoding="utf-8") == "rendered"
    recorded = json.loads((tmp_path / "argv.json").read_text(encoding="utf-8"))
    assert recorded[0] == str(world.binary)
    assert recorded[1] == "-b"
    assert recorded[recorded.index("--python-exit-code") + 1] == "1"
    assert recorded.index("--python-exit-code") < recorded.index("--python")
    assert recorded[recorded.index("--python") + 1] == str(world.package().directory / ENTRY)


def test_run_fails_when_the_blender_script_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path, monkeypatch)
    home = Home.from_env()
    config_module.set_value(home, load_registry(world.registry), "blender.path", str(world.binary))
    (world.package().directory / ENTRY).write_text(FAILING_SCRIPT, encoding="utf-8")
    piece = tmp_path / "piece.toml"
    piece.write_text("[inputs]\n", encoding="utf-8")

    code, envelope = _json(
        capsys,
        "run",
        f"{PACKAGE}.render",
        str(piece),
        "--out",
        str(tmp_path / "out"),
        "--no-verify",
    )

    assert code == EXIT_RUNNER_FAILED
    assert _error(envelope)["code"] == "runner_failed"


# --- FreeCAD: the bundled wrapper and the exit code it passes through.


def test_freecad_uses_the_bundled_wrapper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _world(tmp_path, monkeypatch, app="freecad", app_version="1.1.0", minimum="1.1")
    home = Home.from_env()
    package = world.package()
    config_module.set_value(home, load_registry(world.registry), "freecad.path", str(world.binary))

    argv = runner_module.build_argv(
        package, home, home.env_dir(PACKAGE, "0.1.0"), "run", Path("/in"), Path("/out")
    )

    assert argv[:4] == [
        str(world.binary),
        str(runner_module.FREECAD_WRAPPER),
        "--pass",
        str(package.directory / FREE_ENTRY),
    ]
    assert runner_module.FREECAD_WRAPPER.name == "host_freecad_run.py"
    assert runner_module.FREECAD_WRAPPER.is_file()
    assert argv[4:] == ["--input", "/in", "--result", "/out"]


def test_freecad_wrapper_passes_the_script_exit_code(tmp_path: Path) -> None:
    script = tmp_path / "failing.py"
    script.write_text(FAILING_SCRIPT, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(runner_module.FREECAD_WRAPPER), "--pass", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == SCRIPT_EXIT


def test_freecad_wrapper_runs_under_the_stem_name_freecadcmd_gives_it(tmp_path: Path) -> None:
    """freecadcmd runs a script as ``__name__ == "<stem>"``, never ``__main__``."""
    script = tmp_path / "failing.py"
    script.write_text(FAILING_SCRIPT, encoding="utf-8")
    launch = "import runpy, sys; runpy.run_path(sys.argv[1], run_name='host_freecad_run')"

    result = subprocess.run(
        [sys.executable, "-c", launch, str(runner_module.FREECAD_WRAPPER), "--pass", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == SCRIPT_EXIT


def test_freecad_run_fails_when_the_script_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path, monkeypatch, app="freecad", app_version="1.1.0", minimum="1.1")
    home = Home.from_env()
    config_module.set_value(home, load_registry(world.registry), "freecad.path", str(world.binary))
    (world.package().directory / FREE_ENTRY).write_text(FAILING_SCRIPT, encoding="utf-8")
    piece = tmp_path / "piece.toml"
    piece.write_text("[inputs]\n", encoding="utf-8")

    code, envelope = _json(
        capsys,
        "run",
        f"{PACKAGE}.render",
        str(piece),
        "--out",
        str(tmp_path / "out"),
        "--no-verify",
    )

    assert code == EXIT_RUNNER_FAILED
    assert _error(envelope)["code"] == "runner_failed"


# --- doctor: the exact config fix and the machine_cannot classification.


def test_doctor_reports_the_config_fix_for_a_missing_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    home = Home.from_env()

    checks, _failures, doctor_errors = doctor_module.run_check(
        world.package(), home, "", "host-doctor"
    )

    assert checks["installed"]["ok"] is False
    assert checks["installed"]["fix"] == "facet config set blender.path /path/to/blender"
    assert [error.code for error in doctor_errors] == ["machine_cannot"]
    assert doctor_errors[0].hint == "facet config set blender.path /path/to/blender"
