"""The asset record: one ``<asset>.facet.json`` beside every asset."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
from pathlib import Path

import pytest

from facetmedia.contract import FacetError
from facetmedia.home import Home
from facetmedia.record import (
    RecordSpec,
    asset_id,
    build_record,
    params_digest,
    read_record,
    record_path,
    registry_provenance,
    verify_integrity,
    write_record,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry"
GOLDEN = Path(__file__).resolve().parent / "golden" / "record" / "intro.facet.json"
CREATED = "2026-10-23T01:02:03Z"
AUDIO = b"RIFF\0\0\0\0WAVE"
IMAGE = b"\x89PNG\r\n\x1a\n"
COMMIT = "d" * 40


def _populate(directory: Path) -> Path:
    (directory / "intro.wav").write_bytes(AUDIO)
    (directory / "ref.png").write_bytes(IMAGE)
    return directory


def _spec(directory: Path) -> RecordSpec:
    return RecordSpec(
        name="intro",
        kind="audio",
        mime="audio/wav",
        facet="0.1.0b1",
        capability="kokoro.speech",
        package={"name": "kokoro", "version": "0.1.0", "lock_sha256": "a" * 64},
        tool={"name": "kokoro-onnx", "version": "0.6.1"},
        models=(
            {"id": "kokoro-82m-v1.0-onnx", "sha256": "b" * 64},
            {"id": "faster-whisper-small", "revision": "c" * 40},
        ),
        params={
            "voice": "ef_dora",
            "language": "es",
            "speed": 1.0,
            "text": "Hola.",
            "reference": str(directory / "ref.png"),
        },
        properties={
            "duration_s": 9.641,
            "sample_rate_hz": 24000,
            "lufs": -16.0,
            "peak_dbfs": -1.4,
        },
        inputs=(str(directory / "ref.png"),),
        registry={"commit": COMMIT, "path": None},
        verification={
            "status": "passed",
            "metrics": {"wer": 0.0},
            "thresholds": {"wer_max": 0.05},
            "verifier": "faster-whisper-small@536b0662",
        },
        license={
            "tools": [
                {"subject": "kokoro-onnx", "spdx": "MIT"},
                {"subject": "phonemizer", "spdx": "GPL-3.0-or-later"},
            ],
            "models": [
                {
                    "id": "kokoro-82m-v1.0-onnx",
                    "spdx": "Apache-2.0",
                    "commercial_use": True,
                }
            ],
            "accepted": [],
        },
    )


def _record_for(directory: Path) -> dict[str, object]:
    return build_record(directory / "intro.wav", _spec(directory), created=CREATED)


@pytest.fixture
def asset_dir(tmp_path: Path) -> Path:
    return _populate(tmp_path)


def test_record_matches_golden(asset_dir: Path) -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert _record_for(asset_dir) == golden


def test_write_record_names_the_sibling_and_never_touches_the_asset(asset_dir: Path) -> None:
    before = asset_dir.joinpath("intro.wav").read_bytes()
    record = _record_for(asset_dir)
    target = write_record(asset_dir / "intro.wav", record)
    assert target == asset_dir / "intro.wav.facet.json"
    assert target.is_file()
    assert asset_dir.joinpath("intro.wav").read_bytes() == before
    assert asset_id(asset_dir / "intro.wav") == record["id"]
    assert json.loads(target.read_text(encoding="utf-8")) == record


def test_no_absolute_path_survives(asset_dir: Path) -> None:
    record = _record_for(asset_dir)
    assert str(asset_dir) not in json.dumps(record)
    assert record["path"] == "intro.wav"
    provenance = record["provenance"]
    assert isinstance(provenance, dict)
    assert provenance["inputs"] == [
        {"id": asset_id(asset_dir / "ref.png"), "path": "ref.png"},
    ]
    assert provenance["params"]["reference"] == "ref.png"
    assert provenance["registry"] == {"commit": COMMIT, "path": None}


def test_record_path_accepts_an_asset_or_its_record(tmp_path: Path) -> None:
    asset = tmp_path / "intro.wav"
    expected = tmp_path / "intro.wav.facet.json"
    assert record_path(asset) == expected
    assert record_path(expected) == expected


def test_read_record_works_from_either_path(asset_dir: Path) -> None:
    record = _record_for(asset_dir)
    write_record(asset_dir / "intro.wav", record)
    from_asset = read_record(asset_dir / "intro.wav")
    from_record = read_record(asset_dir / "intro.wav.facet.json")
    assert from_asset == from_record == record


def test_read_record_missing_raises_invalid_input(asset_dir: Path) -> None:
    with pytest.raises(FacetError) as exc:
        read_record(asset_dir / "intro.wav")
    assert exc.value.code == "invalid_input"
    assert "no facet record" in exc.value.message


def test_verify_integrity_tracks_the_asset(asset_dir: Path) -> None:
    asset = asset_dir / "intro.wav"
    write_record(asset, _record_for(asset_dir))
    record = record_path(asset)
    assert verify_integrity(record) is True
    assert verify_integrity(asset) is True
    asset.write_bytes(AUDIO + b"tampered")
    assert verify_integrity(record) is False
    asset.unlink()
    assert verify_integrity(record) is False


def test_verify_integrity_false_without_a_record(asset_dir: Path) -> None:
    assert verify_integrity(asset_dir / "intro.wav") is False


def test_non_commercial_input_makes_the_output_non_commercial(asset_dir: Path) -> None:
    input_asset = asset_dir / "ref.png"
    nc_record = _record_for(asset_dir)
    nc_record["license"] = {"tools": [], "models": [], "commercial_use": False, "accepted": []}
    write_record(input_asset, nc_record)
    inherited = _record_for(asset_dir)
    license_section = inherited["license"]
    assert isinstance(license_section, dict)
    assert license_section["commercial_use"] is False


def test_commercial_input_keeps_the_output_commercial(asset_dir: Path) -> None:
    commercial = _record_for(asset_dir)
    write_record(asset_dir / "ref.png", commercial)
    record = _record_for(asset_dir)
    license_section = record["license"]
    assert isinstance(license_section, dict)
    assert license_section["commercial_use"] is True


def test_input_without_a_record_is_ignored(asset_dir: Path) -> None:
    record = _record_for(asset_dir)
    license_section = record["license"]
    assert isinstance(license_section, dict)
    assert license_section["commercial_use"] is True


def test_non_commercial_model_makes_the_output_non_commercial(asset_dir: Path) -> None:
    nc_model = [{"id": "omnivoice", "spdx": "CC-BY-NC-4.0", "commercial_use": False}]
    spec = dataclasses.replace(
        _spec(asset_dir), license={"tools": [], "models": nc_model, "accepted": []}
    )
    record = build_record(asset_dir / "intro.wav", spec, created=CREATED)
    license_section = record["license"]
    assert isinstance(license_section, dict)
    assert license_section["commercial_use"] is False


def test_accepted_keeps_only_the_documented_fields(asset_dir: Path) -> None:
    accepted = [
        {
            "model": "omnivoice",
            "spdx": "CC-BY-NC-4.0",
            "terms_url": "https://example.com/terms",
            "commercial_use": False,
            "by": "someone",
            "at": CREATED,
            "method": "interactive",
        }
    ]
    spec = dataclasses.replace(
        _spec(asset_dir), license={"tools": [], "models": [], "accepted": accepted}
    )
    record = build_record(asset_dir / "intro.wav", spec, created=CREATED)
    license_section = record["license"]
    assert isinstance(license_section, dict)
    assert license_section["accepted"] == [
        {
            "model": "omnivoice",
            "spdx": "CC-BY-NC-4.0",
            "by": "someone",
            "at": CREATED,
            "method": "interactive",
        }
    ]


def test_params_digest_is_canonical() -> None:
    assert params_digest({"b": [2, 3], "a": 1}) == params_digest({"a": 1, "b": [2, 3]})
    canonical = '{"a":1,"b":[2,3]}'
    assert params_digest({"a": 1, "b": [2, 3]}) == hashlib.sha256(canonical.encode()).hexdigest()


def test_params_digest_keeps_non_ascii_literal() -> None:
    assert params_digest({"t": "ñ"}) == hashlib.sha256('{"t":"ñ"}'.encode()).hexdigest()


def test_verification_status_must_be_known(asset_dir: Path) -> None:
    spec = dataclasses.replace(_spec(asset_dir), verification={"status": "maybe", "metrics": {}})
    with pytest.raises(FacetError) as exc:
        build_record(asset_dir / "intro.wav", spec, created=CREATED)
    assert exc.value.code == "invalid_input"
    assert "status" in exc.value.message


def test_skipped_verification_is_recorded(asset_dir: Path) -> None:
    spec = dataclasses.replace(_spec(asset_dir), verification=None)
    record = build_record(asset_dir / "intro.wav", spec, created=CREATED)
    verification = record["verification"]
    assert isinstance(verification, dict)
    assert verification == {"status": "skipped", "metrics": {}, "thresholds": {}, "verifier": None}


def test_asset_id_of_a_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FacetError) as exc:
        asset_id(tmp_path / "gone.wav")
    assert exc.value.code == "invalid_input"


def test_build_record_of_a_missing_asset_raises(asset_dir: Path) -> None:
    with pytest.raises(FacetError) as exc:
        build_record(asset_dir / "gone.wav", _spec(asset_dir))
    assert exc.value.code == "invalid_input"


def test_created_defaults_to_utc_now(asset_dir: Path) -> None:
    record = build_record(asset_dir / "intro.wav", _spec(asset_dir))
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", str(record["created"]))


def test_relative_params_walk_nested_values(asset_dir: Path) -> None:
    params = {
        "nested": {"ref": str(asset_dir / "ref.png")},
        "list": [str(asset_dir / "ref.png"), "text"],
    }
    spec = dataclasses.replace(_spec(asset_dir), params=params)
    record = build_record(asset_dir / "intro.wav", spec, created=CREATED)
    provenance = record["provenance"]
    assert isinstance(provenance, dict)
    assert provenance["params"] == {"nested": {"ref": "ref.png"}, "list": ["ref.png", "text"]}


def test_a_relative_input_is_named_from_the_record_directory(
    asset_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(asset_dir)
    spec = dataclasses.replace(_spec(asset_dir), params={}, inputs=("ref.png",))
    record = build_record(asset_dir / "intro.wav", spec, created=CREATED)
    provenance = record["provenance"]
    assert isinstance(provenance, dict)
    assert provenance["inputs"] == [{"id": asset_id(asset_dir / "ref.png"), "path": "ref.png"}]


def test_a_relative_input_points_at_the_same_file_as_its_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The id and the stored path describe one file, from the record's side."""
    out = tmp_path / "out"
    refs = tmp_path / "refs"
    out.mkdir()
    refs.mkdir()
    (out / "intro.wav").write_bytes(AUDIO)
    (refs / "ref.png").write_bytes(IMAGE)
    monkeypatch.chdir(tmp_path)
    spec = dataclasses.replace(_spec(out), params={}, inputs=("refs/ref.png",))
    record = build_record(out / "intro.wav", spec, created=CREATED)
    provenance = record["provenance"]
    assert isinstance(provenance, dict)
    assert provenance["inputs"] == [{"id": asset_id(refs / "ref.png"), "path": "../refs/ref.png"}]


def test_input_record_without_a_license_section_is_ignored(asset_dir: Path) -> None:
    write_record(asset_dir / "ref.png", {"license": "nope"})
    record = _record_for(asset_dir)
    license_section = record["license"]
    assert isinstance(license_section, dict)
    assert license_section["commercial_use"] is True


def test_a_corrupt_input_record_is_refused_not_read_as_permissive(asset_dir: Path) -> None:
    (asset_dir / "ref.png.facet.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(FacetError) as exc:
        _record_for(asset_dir)
    assert exc.value.code == "invalid_input"
    assert "corrupt facet record" in exc.value.message


def test_a_corrupt_record_is_not_read_as_a_record(asset_dir: Path) -> None:
    asset = asset_dir / "intro.wav"
    record_path(asset).write_text("{not json", encoding="utf-8")
    with pytest.raises(FacetError) as exc:
        read_record(asset)
    assert exc.value.code == "invalid_input"
    assert "corrupt facet record" in exc.value.message
    assert verify_integrity(asset) is False


def test_a_record_that_is_not_an_object_is_refused(asset_dir: Path) -> None:
    asset = asset_dir / "intro.wav"
    record_path(asset).write_text("[]", encoding="utf-8")
    with pytest.raises(FacetError) as exc:
        read_record(asset)
    assert exc.value.code == "invalid_input"
    assert "not a facet record" in exc.value.message


def test_an_unreadable_record_raises_invalid_input(asset_dir: Path) -> None:
    asset = asset_dir / "intro.wav"
    record_path(asset).mkdir()
    with pytest.raises(FacetError) as exc:
        read_record(asset)
    assert exc.value.code == "invalid_input"
    assert "cannot read facet record" in exc.value.message


def test_malformed_license_lists_become_empty(asset_dir: Path) -> None:
    spec = dataclasses.replace(_spec(asset_dir), license={"tools": "nope", "models": None})
    record = build_record(asset_dir / "intro.wav", spec, created=CREATED)
    license_section = record["license"]
    assert isinstance(license_section, dict)
    assert license_section["tools"] == []
    assert license_section["models"] == []
    assert license_section["accepted"] == []


def test_registry_provenance_uses_the_recorded_snapshot(
    monkeypatch: pytest.MonkeyPatch, asset_dir: Path
) -> None:
    monkeypatch.delenv("FACET_REGISTRY", raising=False)
    home = Home.peek_env()
    home.state_file("registry.json").parent.mkdir(parents=True, exist_ok=True)
    home.state_file("registry.json").write_text(
        json.dumps({"commit": COMMIT, "ref": "main"}), encoding="utf-8"
    )
    assert registry_provenance() == {"commit": COMMIT, "path": None}


def test_registry_provenance_names_a_development_directory(
    monkeypatch: pytest.MonkeyPatch, asset_dir: Path
) -> None:
    monkeypatch.setenv("FACET_REGISTRY", str(FIXTURES))
    assert registry_provenance() == {"commit": None, "path": str(FIXTURES)}


def test_a_relative_development_registry_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACET_REGISTRY", "relative/registry")
    with pytest.raises(FacetError) as exc:
        registry_provenance()
    assert exc.value.code == "invalid_input"
    assert "absolute" in exc.value.message


def test_development_registry_path_is_relative_in_the_record(
    monkeypatch: pytest.MonkeyPatch, asset_dir: Path
) -> None:
    monkeypatch.setenv("FACET_REGISTRY", str(FIXTURES))
    spec = dataclasses.replace(_spec(asset_dir), registry=None)
    record = build_record(asset_dir / "intro.wav", spec, created=CREATED)
    provenance = record["provenance"]
    assert isinstance(provenance, dict)
    section = provenance["registry"]
    assert isinstance(section, dict)
    assert section["commit"] is None
    assert section["path"] == Path(os.path.relpath(FIXTURES, asset_dir)).as_posix()
    assert not section["path"].startswith("/")
