"""``facet example``: copy a capability's bundled example into a folder.

A package ships working pieces under ``examples/`` in the registry snapshot.
This verb copies the first one of a capability into a folder, with every
file and folder its inputs reference, so a person has something to run and
preview. With ``--run`` it also runs the copy and previews the first asset.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from facetmedia import preview as preview_module
from facetmedia import run as run_module
from facetmedia import schema
from facetmedia.contract import FacetError, emit, ok
from facetmedia.registry import Capability, Package, load_active_registry

__all__ = ["Example", "add_flags", "execute", "find_examples", "handle"]


def add_flags(parser: argparse.ArgumentParser) -> None:
    """The ``facet example`` arguments: capability, dest and the copy/run flags."""
    parser.add_argument(
        "capability",
        metavar="<capability>",
        help="a capability id such as deck.slides, from `facet list --available`",
    )
    parser.add_argument(
        "dest",
        nargs="?",
        default=None,
        metavar="<folder>",
        help="folder to copy the example into (default: ./<capability with dots as dashes>)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print every example of the capability instead of copying",
    )
    parser.add_argument(
        "--name",
        default=None,
        metavar="NAME",
        help="copy the example with this file stem",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="run the copied piece after copying, and preview the first asset",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="print the preview URL without opening the system browser",
    )
    parser.add_argument(
        "--out",
        default=None,
        metavar="DIR",
        help="asset directory for --run (default: <dest>/out)",
    )


def handle(args: argparse.Namespace) -> int:
    """Copy a bundled example; ``--run`` also runs it and previews the asset."""
    data, code = execute(args, args.run_id)
    emit(ok(data), args.json)
    if not args.json:
        _announce(args, data)
    return code


def _announce(args: argparse.Namespace, data: dict[str, object]) -> None:
    """What was copied and the exact next command, as ``facet run`` announces."""
    listed = data.get("examples")
    if isinstance(listed, list):
        for entry in listed:
            if isinstance(entry, dict):
                print(entry.get("path"))
        return
    if bool(getattr(args, "run", False)) and "run" in data:
        return
    _announce_copy(args, data)


def _announce_copy(args: argparse.Namespace, data: dict[str, object]) -> None:
    """The copied files and the exact next command, before any run output."""
    files = data.get("files")
    for name in files if isinstance(files, list) else []:
        if isinstance(name, str):
            print(name)
    dest = data.get("dest")
    piece = data.get("piece")
    out = args.out if getattr(args, "out", None) is not None else f"{dest}/out"
    print(f"facet run {data.get('capability')} {dest}/{piece} --out {out}")


@dataclass(frozen=True)
class Example:
    """One ``examples/`` TOML assigned to a capability."""

    capability: str
    path: Path
    piece: str


def find_examples(package: Package) -> list[Example]:
    """Every example TOML of a package, in path order, assigned to a capability."""
    root = package.directory / "examples"
    candidates = sorted(path for path in root.rglob("*.toml") if path.is_file())
    found: list[Example] = []
    for path in candidates:
        capability = _assign(path, package.capabilities)
        if capability is not None:
            found.append(Example(capability=capability, path=path, piece=path.stem))
    return found


def _assign(path: Path, capabilities: tuple[Capability, ...]) -> str | None:
    """The capability id a TOML is an example of, or ``None`` when it is not one."""
    try:
        with path.open("rb") as handle:
            loaded = tomllib.load(handle)
    except OSError:
        return None
    except tomllib.TOMLDecodeError:
        return None
    named = loaded.get("capability")
    if named is not None:
        return _assign_named(named, capabilities)
    for capability in capabilities:
        if path.stem == capability.id.split(".")[-1]:
            return capability.id
    return _assign_validated(loaded, path.parent, capabilities)


def _assign_named(named: object, capabilities: tuple[Capability, ...]) -> str | None:
    """Rule 1: a top-level ``capability`` key names the capability, else not an example."""
    if not isinstance(named, str):
        return None
    for capability in capabilities:
        if capability.id == named:
            return capability.id
    return None


def _assign_validated(
    loaded: dict[str, Any], piece_dir: Path, capabilities: tuple[Capability, ...]
) -> str | None:
    """Rule 3: the inputs validate against exactly one capability."""
    raw = loaded.get("inputs", {})
    if not isinstance(raw, dict):
        return None
    matched: list[str] = []
    for capability in capabilities:
        try:
            schema.validate(capability, raw, piece_dir)
        except FacetError:
            continue
        matched.append(capability.id)
    return matched[0] if len(matched) == 1 else None


def execute(args: argparse.Namespace, run_id: str) -> tuple[dict[str, object], int]:
    """Copy one example, optionally run it and preview the first asset."""
    registry = load_active_registry()
    capability = registry.capability(str(args.capability))
    package = registry.package(capability.package)
    if package is None:
        raise FacetError("internal", f"{capability.id}: its package is not in the registry")
    examples = [item for item in find_examples(package) if item.capability == capability.id]
    if not examples:
        raise _no_example_error(capability, package)
    if bool(getattr(args, "list", False)):
        return _list_data(capability, examples), 0
    chosen = _choose(examples, capability, getattr(args, "name", None))
    dest = _prepare_dest(args, capability)
    files = _copy(chosen, capability, dest)
    data: dict[str, object] = {
        "capability": capability.id,
        "dest": str(dest.resolve()),
        "piece": chosen.path.name,
        "files": files,
    }
    if not bool(getattr(args, "run", False)):
        return data, 0
    if not bool(getattr(args, "json", False)):
        _announce_copy(args, data)
    return _run_after(args, run_id, capability, dest, chosen, data)


def _no_example_error(capability: Capability, package: Package) -> FacetError:
    """The ``invalid_input`` refusal for a capability without an example."""
    have = sorted({item.capability for item in find_examples(package)})
    return FacetError(
        "invalid_input",
        f"{capability.id} has no example; examples exist for: {', '.join(have)}",
        details={"capability": capability.id, "examples": have},
    )


def _list_data(capability: Capability, examples: list[Example]) -> dict[str, object]:
    """The ``--list`` data: every example of the capability."""
    return {
        "capability": capability.id,
        "examples": [{"piece": item.piece, "path": str(item.path)} for item in examples],
    }


def _choose(examples: list[Example], capability: Capability, name: object) -> Example:
    """The example to copy: ``--name`` picks one, else the first in path order."""
    if name is None:
        return examples[0]
    wanted = str(name)
    for item in examples:
        if item.piece == wanted:
            return item
    known = [item.piece for item in examples]
    raise FacetError(
        "invalid_input",
        f"{wanted} is not an example of {capability.id}; examples: {', '.join(known)}",
        details={"capability": capability.id, "name": wanted, "examples": known},
    )


def _prepare_dest(args: argparse.Namespace, capability: Capability) -> Path:
    """The destination directory, created; a non-empty one is refused."""
    raw = getattr(args, "dest", None)
    dest = Path(str(raw)) if raw is not None else Path(capability.id.replace(".", "-"))
    if dest.is_file() or (dest.is_dir() and any(dest.iterdir())):
        raise FacetError(
            "invalid_input",
            f"{dest}: the destination exists and is not empty",
            details={"dest": str(dest)},
        )
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def _copy(chosen: Example, capability: Capability, dest: Path) -> list[str]:
    """Copy the piece and every file or folder its inputs reference."""
    piece, piece_dir = _read_piece(chosen.path)
    inputs = run_module._piece_inputs(capability, piece, piece_dir)
    dest_piece = dest / chosen.path.name
    dest_piece.write_bytes(chosen.path.read_bytes())
    copied = [dest_piece]
    for source in run_module._input_paths(capability, inputs):
        if source.resolve() == chosen.path.resolve():
            continue
        target = _copy_ref(source, piece_dir, dest)
        if target not in copied:
            copied.append(target)
    base = dest.resolve()
    return sorted(target.resolve().relative_to(base).as_posix() for target in copied)


def _read_piece(path: Path) -> tuple[dict[str, Any], Path]:
    """The piece TOML as a dict plus its directory."""
    with path.open("rb") as handle:
        loaded = tomllib.load(handle)
    return loaded, path.parent


def _copy_ref(source: Path, piece_dir: Path, dest: Path) -> Path:
    """Copy one referenced file or folder, preserving its layout under ``dest``."""
    try:
        relative = source.resolve().relative_to(piece_dir.resolve())
    except (OSError, ValueError):
        relative = Path(source.name)
    target = dest / relative
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True)
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target


def _run_after(
    args: argparse.Namespace,
    run_id: str,
    capability: Capability,
    dest: Path,
    chosen: Example,
    data: dict[str, object],
) -> tuple[dict[str, object], int]:
    """Run the copied piece through ``facet run``, then preview on a TTY."""
    out = _out_dir(args, dest)
    run_args = argparse.Namespace(
        capability=capability.id,
        piece=str(dest / chosen.path.name),
        out=str(out),
        verify=True,
        pull=True,
        timeout=None,
        ephemeral=False,
        wait=bool(getattr(args, "wait", True)),
        wait_timeout=float(getattr(args, "wait_timeout", 3600.0)),
    )
    run_data, code = run_module.execute(run_args, run_id)
    data["run"] = run_data
    if bool(getattr(args, "json", False)):
        return data, code
    _announce_run_assets(run_data)
    if code == 0 and sys.stdout.isatty():
        _preview_first(run_data, args, run_id)
    return data, code


def _out_dir(args: argparse.Namespace, dest: Path) -> Path:
    """The run's asset directory: ``--out``, else ``<dest>/out``."""
    raw = getattr(args, "out", None)
    return Path(str(raw)) if raw is not None else dest / "out"


def _announce_run_assets(run_data: dict[str, object]) -> None:
    """The run's own lines, as ``facet run`` prints them."""
    assets = run_data.get("assets")
    for entry in assets if isinstance(assets, list) else []:
        if not isinstance(entry, dict):
            continue
        verification = entry.get("verification")
        status = (
            verification.get("status", "unknown") if isinstance(verification, dict) else "unknown"
        )
        print(f"{entry.get('path')} {entry.get('name')} {status}")
    print(f"out {run_data.get('out_dir')}")


def _preview_first(run_data: dict[str, object], args: argparse.Namespace, run_id: str) -> None:
    """Preview the run's first asset, exactly as ``facet preview`` shows it."""
    assets = run_data.get("assets")
    first = assets[0] if isinstance(assets, list) and assets else None
    if not isinstance(first, dict) or not isinstance(first.get("path"), str):
        return
    preview_args = argparse.Namespace(
        asset=str(first["path"]),
        json=False,
        no_open=bool(getattr(args, "no_open", False)),
        port=None,
        idle_timeout=600.0,
    )
    preview_module.execute(preview_args, run_id)
