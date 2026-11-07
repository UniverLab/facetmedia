"""Read and validate a package's ``[[models]]`` entries.

``facetmedia.manifest`` keeps the package grammar; the model grammar lives
here so the manifest module stays inside the lab's per-file size cap, the
same split ``facetmedia.manifest_binary`` makes for ``[engine.binary]``.  The
loader passed in is :class:`facetmedia.manifest.RegistryLoader`, which
collects every problem instead of stopping at the first one.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from facetmedia.registry_types import Model

if TYPE_CHECKING:
    from facetmedia.manifest import RegistryLoader

_MODEL_ID_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
_REVISION_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{40}\Z")
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}\Z")
_MODEL_KEYS: Final[frozenset[str]] = frozenset(
    {
        "id",
        "spdx",
        "commercial_use",
        "requires_acceptance",
        "terms_url",
        "url",
        "sha256",
        "bytes",
        "path",
        "hf_repo",
        "revision",
        "backend",
    }
)


def read_models(loader: RegistryLoader, raw: object, file: str) -> tuple[Model, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        loader.add(file, "models", "type")
        return ()
    models: list[Model] = []
    for position, item in enumerate(raw):
        prefix = f"models[{position}]."
        if not isinstance(item, dict):
            loader.add(file, f"{prefix}id", "type")
            continue
        model = _read_model(loader, item, file, prefix)
        if model is not None:
            models.append(model)
    return tuple(models)


def _read_model(
    loader: RegistryLoader, item: Mapping[str, Any], file: str, prefix: str
) -> Model | None:
    before = len(loader.problems)
    loader.unknown(item, _MODEL_KEYS, file, prefix)
    model_id = loader.string(item, "id", file, prefix, required=True)
    if model_id is not None:
        if not _MODEL_ID_RE.fullmatch(model_id):
            loader.add(file, f"{prefix}id", "value")
        elif model_id in loader.model_ids:
            loader.add(file, f"{prefix}id", "duplicate")
        else:
            loader.model_ids[model_id] = file
    spdx = loader.string(item, "spdx", file, prefix, required=True)
    commercial = loader.boolean(item, "commercial_use", file, prefix, default=False)
    acceptance = loader.boolean(item, "requires_acceptance", file, prefix, default=False)
    terms_url = loader.string(item, "terms_url", file, prefix, required=False) or ""
    backend = loader.boolean(item, "backend", file, prefix, default=False)
    if backend:
        size: int | None = _backend_check(loader, item, file, prefix, acceptance, terms_url)
        direct: dict[str, str] = {}
        hf: dict[str, str] = {}
    else:
        size = loader.integer(item, "bytes", file, prefix, required=True)
        if size is not None and size <= 0:
            loader.add(file, f"{prefix}bytes", "value")
        direct, hf = _model_source(loader, item, file, prefix)
    if len(loader.problems) != before:
        return None
    if model_id is None or spdx is None or size is None:
        return None
    if backend:
        return Model(
            id=model_id,
            spdx=spdx,
            commercial_use=commercial,
            requires_acceptance=acceptance,
            bytes=0,
            terms_url=terms_url,
            backend=True,
        )
    return Model(
        id=model_id,
        spdx=spdx,
        commercial_use=commercial,
        requires_acceptance=acceptance,
        bytes=size,
        terms_url=terms_url,
        url=direct.get("url", ""),
        sha256=direct.get("sha256", ""),
        path=direct.get("path", ""),
        hf_repo=hf.get("hf_repo", ""),
        revision=hf.get("revision", ""),
    )


def _backend_check(
    loader: RegistryLoader,
    item: Mapping[str, Any],
    file: str,
    prefix: str,
    acceptance: bool,
    terms_url: str,
) -> int:
    """A backend-owned model names no source: any source key is a value error."""
    for key in ("url", "sha256", "path", "hf_repo", "revision", "bytes"):
        if item.get(key) is not None:
            loader.add(file, f"{prefix}{key}", "value")
    if acceptance and not terms_url:
        loader.add(file, f"{prefix}terms_url", "required")
    return 0


def _model_source(
    loader: RegistryLoader, item: Mapping[str, Any], file: str, prefix: str
) -> tuple[dict[str, str], dict[str, str]]:
    has_url = item.get("url") is not None
    has_hf = item.get("hf_repo") is not None
    if has_url == has_hf:
        loader.add(file, f"{prefix}url", "required" if not has_url else "value")
        return {}, {}
    if has_hf:
        return {}, _hf_source(loader, item, file, prefix)
    return _url_source(loader, item, file, prefix), {}


def _url_source(
    loader: RegistryLoader, item: Mapping[str, Any], file: str, prefix: str
) -> dict[str, str]:
    url = loader.string(item, "url", file, prefix, required=True) or ""
    if url and not url.startswith(("https://", "file://")):
        loader.add(file, f"{prefix}url", "value")
    sha256 = loader.string(item, "sha256", file, prefix, required=True) or ""
    if sha256 and not _SHA256_RE.fullmatch(sha256):
        loader.add(file, f"{prefix}sha256", "value")
    path = loader.string(item, "path", file, prefix, required=True) or ""
    if path and (Path(path).is_absolute() or ".." in Path(path).parts):
        loader.add(file, f"{prefix}path", "value")
    return {"url": url, "sha256": sha256, "path": path}


def _hf_source(
    loader: RegistryLoader, item: Mapping[str, Any], file: str, prefix: str
) -> dict[str, str]:
    repo = loader.string(item, "hf_repo", file, prefix, required=True) or ""
    revision = loader.string(item, "revision", file, prefix, required=True) or ""
    if revision and not _REVISION_RE.fullmatch(revision):
        loader.add(file, f"{prefix}revision", "value")
    return {"hf_repo": repo, "revision": revision}
