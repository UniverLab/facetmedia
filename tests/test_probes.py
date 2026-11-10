"""Probes tests: WSL2 nvidia-smi fallback. No GPU hardware needed."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import facetmedia.probes as probes_module

SINGLE_GPU_MIB = 8151
TWO_GPU_LARGEST_MIB = 24564


def _fake_wsl_home(monkeypatch, tmp_path: Path) -> Path:
    lib = tmp_path / "lib"
    lib.mkdir()
    dxg = tmp_path / "dxg"
    version = tmp_path / "version"
    monkeypatch.setattr(probes_module, "WSL_LIB_PATH", lib)
    monkeypatch.setattr(probes_module, "WSL_DXG_DEVICE", dxg)
    monkeypatch.setattr(probes_module, "WSL_VERSION_SOURCE", version)
    return lib


def _write_stub(path: Path, *, exit_code: int = 0) -> Path:
    if exit_code == 0:
        body = "#!/bin/sh\necho 'Fake GPU, 99 MiB, 1 MiB'\n"
    else:
        body = "#!/bin/sh\nexit 1\n"
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    assert os.access(path, os.X_OK)
    return path


def test_path_stub_wins_over_wsl(monkeypatch, tmp_path) -> None:
    lib = _fake_wsl_home(monkeypatch, tmp_path)
    _write_stub(lib / "nvidia-smi")
    (tmp_path / "dxg").touch()
    (tmp_path / "version").write_text("Microsoft", encoding="utf-8")
    path_stub = tmp_path / "path-smi"
    _write_stub(path_stub)
    monkeypatch.setattr(probes_module.shutil, "which", lambda _name: str(path_stub))
    assert probes_module.gpu_info() == "Fake GPU, 99 MiB, 1 MiB"


def test_path_miss_no_wsl_markers(monkeypatch, tmp_path) -> None:
    lib = _fake_wsl_home(monkeypatch, tmp_path)
    _write_stub(lib / "nvidia-smi")
    (tmp_path / "version").write_text("Linux generic", encoding="utf-8")
    monkeypatch.setattr(probes_module.shutil, "which", lambda _name: None)
    assert probes_module.gpu_info() is None


def test_path_miss_wsl_stub_runs(monkeypatch, tmp_path) -> None:
    lib = _fake_wsl_home(monkeypatch, tmp_path)
    _write_stub(lib / "nvidia-smi")
    (tmp_path / "dxg").touch()
    (tmp_path / "version").write_text("Microsoft", encoding="utf-8")
    monkeypatch.setattr(probes_module.shutil, "which", lambda _name: None)
    assert probes_module.gpu_info() == "Fake GPU, 99 MiB, 1 MiB"


def test_wsl_stub_exit_nonzero(monkeypatch, tmp_path) -> None:
    lib = _fake_wsl_home(monkeypatch, tmp_path)
    _write_stub(lib / "nvidia-smi", exit_code=1)
    (tmp_path / "dxg").touch()
    (tmp_path / "version").write_text("Microsoft", encoding="utf-8")
    monkeypatch.setattr(probes_module.shutil, "which", lambda _name: None)
    assert probes_module.gpu_info() is None


def test_gpu_total_mib_single_gpu(monkeypatch) -> None:
    monkeypatch.setattr(
        probes_module,
        "gpu_info",
        lambda: f"NVIDIA GeForce RTX 5060, {SINGLE_GPU_MIB} MiB, 931 MiB",
    )
    assert probes_module.gpu_total_mib() == SINGLE_GPU_MIB


def test_gpu_total_mib_largest_wins(monkeypatch) -> None:
    monkeypatch.setattr(
        probes_module,
        "gpu_info",
        lambda: f"GPU-A, 8192 MiB, 100 MiB\nGPU-B, {TWO_GPU_LARGEST_MIB} MiB, 200 MiB",
    )
    assert probes_module.gpu_total_mib() == TWO_GPU_LARGEST_MIB


def test_gpu_total_mib_unparsable_line_is_none(monkeypatch) -> None:
    monkeypatch.setattr(probes_module, "gpu_info", lambda: "Fake GPU, N/A, 1 MiB")
    assert probes_module.gpu_total_mib() is None


def test_gpu_total_mib_no_gpu_is_none(monkeypatch) -> None:
    monkeypatch.setattr(probes_module, "gpu_info", lambda: None)
    assert probes_module.gpu_total_mib() is None
