"""The shared machine probes: RAM, disk trees, the GPU and host libraries.

``facetmedia.doctor`` re-exports these names so its callers and tests keep
working; the implementations live here because ``doctor.py`` sits at the
file-size cap.  ``facetmedia.env`` imports them through ``doctor`` — never copy them.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
from pathlib import Path
from typing import Final

from facetmedia.contract import FacetError
from facetmedia.registry_types import Package

NO_HOST_LIBS_HINT: Final[str] = "facet never installs host libraries"
NVIDIA_SMI_TIMEOUT_S: Final[float] = 2.0
NVIDIA_SMI: Final[str] = "nvidia-smi"
WSL_LIB_PATH: Final[Path] = Path("/usr/lib/wsl/lib")
WSL_SMI_NAME: Final[str] = NVIDIA_SMI
WSL_DXG_DEVICE: Final[Path] = Path("/dev/dxg")
WSL_VERSION_MARKER: Final[str] = "microsoft"
WSL_VERSION_SOURCE: Final[Path] = Path("/proc/version")
#: Index of the total-memory column in ``gpu_info()`` CSV lines.
TOTAL_COLUMN: Final[int] = 1


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


def _wsl_detected() -> bool:
    """True when running under WSL: ``/dev/dxg`` exists or version mentions it."""
    try:
        if WSL_DXG_DEVICE.exists():
            return True
        text = WSL_VERSION_SOURCE.read_text(encoding="utf-8")
    except OSError:
        return False
    return WSL_VERSION_MARKER in text.lower()


def _resolve_smi() -> str | None:
    """Path to ``nvidia-smi``: PATH first, then the WSL2 driver path."""
    found = shutil.which(NVIDIA_SMI)
    if found:
        return found
    candidate = WSL_LIB_PATH / WSL_SMI_NAME
    if _wsl_detected() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def gpu_info() -> str | None:
    """One line per GPU from ``nvidia-smi``, or ``None`` when there is none.

    Falls back to the WSL2 driver path (``/usr/lib/wsl/lib/nvidia-smi``)
    when ``nvidia-smi`` is not on PATH.
    """
    resolved = _resolve_smi()
    if resolved is None:
        return None
    try:
        completed = subprocess.run(
            [
                resolved,
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


def total_mib(info: str | None) -> int | None:
    """Largest GPU total memory in MiB from a ``gpu_info()`` string.

    Parses the second CSV field of each line (``name, total MiB, used MiB``)
    and returns the largest total; ``None`` when ``info`` is missing or no
    line parses.
    """
    if not info:
        return None
    totals: list[int] = []
    for line in info.splitlines():
        fields = line.split(",")
        if len(fields) <= TOTAL_COLUMN:
            continue
        try:
            totals.append(int(fields[TOTAL_COLUMN].split()[0]))
        except (IndexError, ValueError):
            continue
    return max(totals) if totals else None


def gpu_total_mib() -> int | None:
    """Largest GPU total memory in MiB, or ``None`` when there is no GPU."""
    return total_mib(gpu_info())


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
