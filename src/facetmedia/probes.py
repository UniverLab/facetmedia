"""The shared machine probes: RAM, disk trees, the GPU and host libraries.

``facetmedia.doctor`` re-exports these names so its callers and tests keep
working; the implementations live here because ``doctor.py`` sits at the
file-size cap.  ``facetmedia.env`` imports them through ``doctor`` — never copy them.
"""

from __future__ import annotations

import ctypes
import subprocess
from pathlib import Path
from typing import Final

from facetmedia.contract import FacetError
from facetmedia.registry_types import Package

NO_HOST_LIBS_HINT: Final[str] = "facet never installs host libraries"
NVIDIA_SMI_TIMEOUT_S: Final[float] = 2.0


def mem_total_gb() -> float | None:
    """Total RAM from ``/proc/meminfo`` in GB, or ``None`` when unreadable."""
    return _meminfo_gb("MemTotal")


def mem_available_gb() -> float | None:
    """Available RAM from ``/proc/meminfo`` in GB, or ``None`` when unknown."""
    return _meminfo_gb("MemAvailable")


def _meminfo_gb(key: str) -> float | None:
    try:
        text = Path("/proc/meminfo").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        if name != key:
            continue
        try:
            return int(rest.split()[0]) / (1024 * 1024)
        except (IndexError, ValueError):
            return None
    return None


def tree_bytes(path: Path) -> int:
    """Bytes used by the files under ``path``; 0 when it does not exist."""
    if not path.exists():
        return 0
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def gpu_info() -> str | None:
    """One line per GPU from ``nvidia-smi``, or ``None`` when there is none."""
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=NVIDIA_SMI_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    text = completed.stdout.strip()
    return text or None


def host_lib_error(package: Package) -> FacetError | None:
    """The first soname that does not load, or ``None`` when all load."""
    for soname in package.requirements.host_libs:
        try:
            ctypes.CDLL(soname)
        except OSError as exc:
            return FacetError(
                "machine_cannot",
                f"cannot load {soname}: {exc}; {NO_HOST_LIBS_HINT}",
                hint=package.requirements.host_hint or None,
                details={"soname": soname},
            )
    return None
