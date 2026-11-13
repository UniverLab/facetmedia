"""Command-line interface for facet."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import traceback
from pathlib import Path

from facetmedia import (
    check,
    config,
    contract,
    doctor,
    example,
    introspect,
    license_accept,
    licenses,
    lock,
    plan,
    preview,
    progress,
    prune,
    record,
    registry,
    run,
    update,
    updates,
)
from facetmedia.cli_parser import FacetParser, Handler
from facetmedia.cli_parser import build_parser as _build_parser
from facetmedia.contract import FacetError
from facetmedia.home import Home, log_file, new_run_id, require_posix
from facetmedia.registry import load_active_registry, validate_registry


def _lock_params(args: argparse.Namespace) -> tuple[bool, float]:
    """``(wait, timeout_s)`` for a verb that takes the facet lock."""
    wait = getattr(args, "wait", True)
    timeout_s = getattr(args, "wait_timeout", 3600.0)
    return bool(wait), float(timeout_s)


def _handle_license_list(args: argparse.Namespace) -> int:
    rows = licenses.list_status()
    contract.emit(contract.ok({"models": rows}), args.json)
    if not args.json:
        for row in rows:
            state = "accepted" if row["accepted"] else "not accepted"
            print(f"{row['id']} {row['spdx']} {state}")
    return 0


_LICENSE_HANDLERS: dict[str, Handler] = {
    "accept": license_accept.handle,
    "list": _handle_license_list,
}


def _handle_config_set(args: argparse.Namespace) -> int:
    """Store one host application path; stdout carries only the JSON envelope."""
    data = config.set_value(Home.from_env(), load_active_registry(), args.key, args.value)
    contract.emit(contract.ok(data), True)
    return 0


def _handle_config_get(args: argparse.Namespace) -> int:
    data = config.get_value(Home.from_env(), load_active_registry(), args.key)
    contract.emit(contract.ok(data), True)
    return 0


def _handle_config_list(args: argparse.Namespace) -> int:
    data = config.list_values(Home.from_env(), load_active_registry())
    contract.emit(contract.ok(data), True)
    return 0


_CONFIG_HANDLERS: dict[str, Handler] = {
    "set": _handle_config_set,
    "get": _handle_config_get,
    "list": _handle_config_list,
}


def _handle_update(args: argparse.Namespace) -> int:
    _configure_output(args)
    ref = args.ref or update.default_ref()
    ref_source = _ref_source(args)
    try:
        with lock.hold("exclusive", "facet update", *_lock_params(args)):
            data = update.update_registry(Home.from_env(), ref, ref_source=ref_source)
    except FacetError:
        progress.announce_failure(getattr(args, "run_id", ""))
        raise
    contract.emit(contract.ok(data), args.json)
    if not args.json:
        print(f"registry {data['commit']} (ref {data['ref']})")
        for package in _packages(data):
            suffix = " changed" if package["changed"] else ""
            print(f"  {package['name']} {package['version']}{suffix}")
    return 0


def _ref_source(args: argparse.Namespace) -> str:
    """Where the ref came from, so a failure can name it instead of guessing."""
    if args.ref is not None:
        return "explicit"
    if os.environ.get("FACET_REGISTRY_REF"):
        return "default:env"
    return "default:builtin"


def _packages(data: dict[str, object]) -> list[dict[str, object]]:
    packages = data.get("packages")
    if not isinstance(packages, list):
        return []
    return [package for package in packages if isinstance(package, dict)]


def _handle_pull(args: argparse.Namespace) -> int:
    _configure_output(args)
    return plan.handle_pull(args)


def _configure_output(args: argparse.Namespace) -> None:
    """Make the detailed working log verbose-only and build the pull reporter."""
    loud = bool(getattr(args, "verbose", False))
    progress.set_verbose(loud)
    args.progress = progress.Progress.for_cli(
        as_json=bool(args.json),
        verbose=loud,
        stream=sys.stderr,
    )


def _handle_run(args: argparse.Namespace) -> int:
    """The envelope carries every record; the exit code reports verification."""
    data, exit_code = run.execute(args, args.run_id)
    contract.emit(contract.ok(data), args.json)
    if not args.json:
        _announce_run(data)
    return exit_code


def _announce_run(data: dict[str, object]) -> None:
    assets = data.get("assets")
    for entry in assets if isinstance(assets, list) else []:
        if not isinstance(entry, dict):
            continue
        verification = entry.get("verification")
        status = (
            verification.get("status", "unknown") if isinstance(verification, dict) else "unknown"
        )
        print(f"{entry.get('path')} {entry.get('name')} {status}")
    print(f"out {data.get('out_dir')}")


def _handle_check(args: argparse.Namespace) -> int:
    """Content only: one asset, the lock per its manifest, re-verification."""
    home = Home.from_env()
    capability = _check_capability(args.asset)
    _capability, package = check.resolve(capability)
    mode: lock.LockMode = "exclusive" if package.requirements.exclusive else "shared"
    wait, wait_timeout = _lock_params(args)
    with lock.hold(mode, "facet check", wait, wait_timeout):
        data, code = check.reverify(args.asset, home, args.run_id)
    contract.emit(contract.ok(data), args.json)
    if not args.json:
        _announce_reverify(data)
    return code


def _check_capability(asset: str) -> str:
    """The record's capability; a capability id is a doctor target, not an asset."""
    path = Path(asset)
    bare = path
    if path.name.endswith(record.RECORD_SUFFIX):
        bare = Path(str(path)[: -len(record.RECORD_SUFFIX)])
    if path.is_file() or bare.is_file():
        return check.record_capability(asset)
    if doctor.is_known_target(asset, Home.from_env()):
        raise FacetError(
            "invalid_input",
            f"{asset} is a capability id; facet check re-verifies an asset",
            hint=f"facet doctor {asset}",
            details={"target": asset},
        )
    raise FacetError("invalid_input", f"{asset}: no such asset file")


def _announce_reverify(data: dict[str, object]) -> None:
    state = "passed" if data.get("passed") is True else "failed"
    print(f"{data.get('asset')} {state}")


def _handle_doctor(args: argparse.Namespace) -> int:
    """The tool's doctor: the facet section plus each package's doctor."""
    if args.quick and args.full:
        raise FacetError("invalid_input", "--quick and --full cannot be combined")
    depth = "full" if args.full else "quick"
    data, code = doctor.execute(args.target, depth, args.run_id, *_lock_params(args))
    ok = data.get("healthy") is True
    envelope = contract.ok(data)
    envelope["ok"] = ok
    contract.emit(envelope, args.json)
    if not args.json:
        _announce_doctor(data, depth)
    return 0 if ok else code


def _announce_doctor(data: dict[str, object], depth: str) -> None:
    state = "healthy" if data.get("healthy") is True else "unhealthy"
    capability = data.get("capability")
    prefix = f"{capability}: " if isinstance(capability, str) else ""
    suffix = ""
    if data.get("needs_pull") is True:
        suffix = ", needs pull"
    print(f"{prefix}{state}{suffix}")
    print(f"depth: {depth}")
    if depth == "quick":
        print("--full also starts each package's server and runs it once")
    facet = data.get("facet")
    if isinstance(facet, dict) and facet:
        print("facet:")
        _announce_checks(facet)
    packages = data.get("packages")
    for name, section in packages.items() if isinstance(packages, dict) else []:
        if not isinstance(section, dict):
            continue
        mark = "installed" if section.get("installed") is True else "not installed"
        print(f"{name} ({mark}):")
        readiness = section.get("readiness")
        if isinstance(readiness, dict):
            _announce_checks(readiness)
        doctor_checks = section.get("doctor")
        if isinstance(doctor_checks, list):
            _announce_checks({str(entry.get("name", "doctor")): entry for entry in doctor_checks})


def _announce_checks(checks: dict[str, object]) -> None:
    """One ``✓/✗ name: detail`` line per check, with its fix when it failed."""
    for name, entry in checks.items():
        if not isinstance(entry, dict):
            continue
        mark = "✓" if entry.get("ok") is True else "✗"
        print(f"  {mark} {name}: {entry.get('detail')}")
        fix = entry.get("fix")
        if mark == "✗" and isinstance(fix, str) and fix:
            print(f"    → fix: {fix}")


def _handle_preview(args: argparse.Namespace) -> int:
    """A light verb: no lock; ``--json`` prints data, otherwise serve it."""
    data, code = preview.execute(args, args.run_id)
    contract.emit(contract.ok(data), args.json)
    return code


def _handle_rm(args: argparse.Namespace) -> int:
    """Exclusive: delete a package's environments, and with ``--models`` its models."""
    with lock.hold("exclusive", "facet remove", *_lock_params(args)):
        active = load_active_registry() if args.models else None
        data = prune.rm_package(
            Home.from_env(),
            args.package,
            with_models=bool(args.models),
            registry=active,
        )
    contract.emit(contract.ok(data), args.json)
    if not args.json:
        _announce_rm(data)
    return 0


def _handle_gc(args: argparse.Namespace) -> int:
    """Exclusive: remove what nothing uses any more, or list it with ``--dry-run``."""
    with lock.hold("exclusive", "facet clean", *_lock_params(args)):
        data = prune.gc_sweep(
            Home.from_env(),
            load_active_registry(),
            dry_run=bool(args.dry_run),
        )
    contract.emit(contract.ok(data), args.json)
    if not args.json:
        _announce_gc(data)
    return 0


def _announce_rm(data: dict[str, object]) -> None:
    removed = data.get("removed")
    paths = [path for path in removed if isinstance(path, str)] if isinstance(removed, list) else []
    kept = data.get("kept")
    kept_paths = [path for path in kept if isinstance(path, str)] if isinstance(kept, list) else []
    if not paths and not kept_paths:
        print("nothing to remove")
        return
    for path in paths:
        print(path)
    print(f"freed {data.get('freed_bytes')} bytes")
    for path in kept_paths:
        print(f"kept {path} (the package's own data; delete it by hand to discard it)")


def _announce_gc(data: dict[str, object]) -> None:
    items = data.get("items")
    for entry in items if isinstance(items, list) else []:
        if isinstance(entry, dict):
            print(f"{entry.get('path')} {entry.get('bytes')} bytes {entry.get('reason')}")
    verb = "would free" if data.get("dry_run") is True else "freed"
    print(f"{verb} {data.get('freed_bytes')} bytes")


def _handle_list(args: argparse.Namespace) -> int:
    """A light verb: the registry snapshot and the home, no lock.

    The update check this invocation may have run is the only network it
    touches, and its answer never changes this output.
    """
    data = introspect.list_data(args.available, args.facet)
    contract.emit(contract.ok(data), args.json)
    if not args.json:
        introspect.announce_list(data, args.available, args.facet)
    return 0


def _handle_describe(args: argparse.Namespace) -> int:
    data = introspect.describe_data(args.capability)
    contract.emit(contract.ok(data), args.json)
    if not args.json:
        _announce_describe(data)
    return 0


def _announce_describe(data: dict[str, object]) -> None:
    print(f"summary: {data['summary']}")
    package = data.get("package")
    if isinstance(package, dict):
        print(
            f"package: {package.get('name')} {package.get('version')} "
            f"({package.get('tool_name')}, {package.get('provider')})"
        )
    print(f"inputs: {_describe_inputs(data)}")
    print(f"installed: {'yes' if data.get('installed') is True else 'no'}")


def _describe_inputs(data: dict[str, object]) -> str:
    """One ``name (type, required|default X)`` fragment per input field."""
    inputs = data.get("inputs")
    properties = inputs.get("properties") if isinstance(inputs, dict) else None
    required = inputs.get("required") if isinstance(inputs, dict) else None
    parts: list[str] = []
    for name, field_schema in properties.items() if isinstance(properties, dict) else []:
        if not isinstance(field_schema, dict):
            continue
        kind = field_schema.get("type", "?")
        if isinstance(required, list) and name in required:
            parts.append(f"{name} ({kind}, required)")
        elif "default" in field_schema:
            parts.append(f"{name} ({kind}, default {field_schema['default']})")
        else:
            parts.append(f"{name} ({kind})")
    return ", ".join(parts)


def _handle_skill(args: argparse.Namespace) -> int:
    """A light verb: print a skill plus what is true on this machine."""
    data = introspect.skill_data(
        args.target, args.reference, with_doctor=not args.no_doctor, run_id=args.run_id
    )
    contract.emit(contract.ok(data), args.json)
    if not args.json:
        sys.stdout.write(str(data["body"]))
    return 0


_HANDLERS: dict[str, Handler] = {
    "check": _handle_check,
    "describe": _handle_describe,
    "doctor": _handle_doctor,
    "clean": _handle_gc,
    "example": example.handle,
    "list": _handle_list,
    "preview": _handle_preview,
    "pull": _handle_pull,
    "remove": _handle_rm,
    "run": _handle_run,
    "skill": _handle_skill,
    "update": _handle_update,
}


def _handle_registry_validate(args: argparse.Namespace) -> int:
    path = Path(args.dir)
    problems = validate_registry(path)
    if not problems:
        if args.json:
            package_names = sorted(registry.load_registry(path).packages.keys())
            contract.emit(
                contract.ok({"path": str(path), "valid": True, "packages": package_names}),
                True,
            )
        return 0
    for problem in problems:
        sys.stderr.write(f"{problem['file']}: {problem['key']}: {problem['problem']}\n")
    if args.json:
        contract.emit(
            contract.failure(
                FacetError(
                    "invalid_input", "registry validation failed", details={"errors": problems}
                )
            ),
            True,
        )
    return 2


_EXAMPLES: dict[tuple[str, ...], tuple[str, ...]] = {
    ("update",): ("facet update", "facet update --ref v0.1.0", "facet update --json"),
    ("pull",): ("facet pull image", "facet pull --package comfy", "facet pull --plan voice"),
    ("list",): ("facet list", "facet list --available", "facet list --facet image"),
    ("describe",): ("facet describe ffmpeg.normalize", "facet describe kokoro.speak --json"),
    ("example",): ("facet example deck.slides", "facet example deck.slides my-talk --run"),
    ("run",): (
        "facet run deck.slides talk.toml --out out",
        "facet run ffmpeg.normalize normalize.toml",
        "facet run kokoro.speak talk.toml --json",
    ),
    ("check",): ("facet check out/asset.png", "facet check out/asset.png --json"),
    ("preview",): (
        "facet preview out/asset.png",
        "facet preview out",
        "facet preview out --no-open",
    ),
    ("doctor",): ("facet doctor", "facet doctor ffmpeg.normalize", "facet doctor --full"),
    ("remove",): ("facet remove comfy", "facet remove comfy --models"),
    ("clean",): ("facet clean", "facet clean --dry-run"),
    ("skill",): ("facet skill", "facet skill comfy"),
    ("license",): ("facet license list", "facet license accept omnivoice"),
    ("config",): (
        "facet config list",
        "facet config set blender.path /usr/bin/blender",
        "facet config get blender.path",
    ),
    ("registry",): (
        "facet registry validate ../registry",
        "facet registry validate ../registry --json",
    ),
    ("license", "accept"): (
        "facet license accept omnivoice",
        "facet license accept omnivoice --yes",
    ),
    ("license", "list"): ("facet license list", "facet license list --json"),
    ("config", "set"): (
        "facet config set blender.path /usr/bin/blender",
        "facet config set blender.path ~/.local/bin/blender",
    ),
    ("config", "get"): ("facet config get blender.path", "facet config get blender.path --json"),
    ("config", "list"): ("facet config list", "facet config list --json"),
    ("registry", "validate"): (
        "facet registry validate ../registry",
        "facet registry validate ../registry --json",
    ),
}


def build_parser() -> FacetParser:
    """The parser for this module's handlers and its examples table."""
    return _build_parser(
        handlers=_HANDLERS,
        license_handlers=_LICENSE_HANDLERS,
        config_handlers=_CONFIG_HANDLERS,
        registry_validate=_handle_registry_validate,
        examples=_EXAMPLES,
    )


def main(argv: list[str] | None = None) -> int:
    run_id = new_run_id()
    as_json = "--json" in (argv if argv is not None else sys.argv)
    was_verbose = progress.verbose()
    try:
        require_posix()
        Home.from_env()
        return _run_logged(argv, run_id)
    except FacetError as exc:
        contract.emit(contract.failure(exc), as_json)
        return exc.exit_code
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc(file=sys.stderr)
        fallback = FacetError("internal", "internal error, see stderr", details={"log": "stderr"})
        contract.emit(contract.failure(fallback), as_json)
        return 1
    finally:
        progress.set_verbose(was_verbose)


def _run_logged(argv: list[str] | None, run_id: str) -> int:
    as_json = "--json" in (argv if argv is not None else sys.argv)
    path = log_file(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("facetmedia")
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        try:
            args = build_parser().parse_args(argv)
            args.run_id = run_id
            as_json = bool(getattr(args, "json", as_json))
            handler_fn: Handler = args.handler
            notices: list[dict[str, object]] = []
            contract.set_notices(())
            if getattr(args, "command", "") in updates.CHECK_VERBS:
                notices = updates.begin(Home.from_env())
                contract.set_notices(notices)
            code = handler_fn(args)
            try:
                updates.print_notice(notices, as_json)
            except Exception:
                logger.exception("update notice failed")
            return code
        except FacetError as exc:
            contract.emit(contract.failure(exc), as_json)
            return exc.exit_code
        except SystemExit:
            raise
        except Exception:
            logger.exception("unhandled exception")
            fallback = FacetError(
                "internal",
                f"internal error, see {path}",
                details={"log": str(path)},
            )
            contract.emit(contract.failure(fallback), as_json)
            return 1
    finally:
        logger.removeHandler(handler)
        handler.close()
        logger.setLevel(previous_level)
