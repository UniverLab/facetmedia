import json
from pathlib import Path

import pytest

from facetmedia.contract import FacetError
from facetmedia.registry import Field, load_registry
from facetmedia.schema import to_json_schema, validate

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "registry"
GOLDEN_ROOT = Path(__file__).parent / "golden" / "schema"
REPEAT_MAX = 10


def test_validate_fills_defaults() -> None:
    registry = load_registry(FIXTURE_ROOT)
    cap = registry.capability("echo.text")
    piece = {"text": "hello"}
    result = validate(cap, piece, Path.cwd())
    assert result == {"text": "hello", "repeat": 1}


def test_validate_required_field_missing_raises() -> None:
    registry = load_registry(FIXTURE_ROOT)
    cap = registry.capability("echo.text")
    piece = {}
    with pytest.raises(FacetError) as exc:
        validate(cap, piece, Path.cwd())
    assert exc.value.code == "invalid_input"
    errors = exc.value.details["errors"]
    assert any(e["field"] == "text" and e["problem"] == "required" for e in errors)


def test_validate_unknown_key_raises() -> None:
    registry = load_registry(FIXTURE_ROOT)
    cap = registry.capability("echo.text")
    piece = {"text": "hello", "unknown": "value"}
    with pytest.raises(FacetError) as exc:
        validate(cap, piece, Path.cwd())
    assert exc.value.code == "invalid_input"
    errors = exc.value.details["errors"]
    assert any(e["field"] == "unknown" and e["problem"] == "unknown" for e in errors)


def test_validate_type_check_string() -> None:
    registry = load_registry(FIXTURE_ROOT)
    cap = registry.capability("echo.text")
    piece = {"text": 123}
    with pytest.raises(FacetError) as exc:
        validate(cap, piece, Path.cwd())
    assert exc.value.code == "invalid_input"
    errors = exc.value.details["errors"]
    assert any(e["field"] == "text" and e["problem"] == "type" for e in errors)


def test_validate_type_check_integer() -> None:
    registry = load_registry(FIXTURE_ROOT)
    cap = registry.capability("echo.text")
    piece = {"text": "hello", "repeat": "not an int"}
    with pytest.raises(FacetError) as exc:
        validate(cap, piece, Path.cwd())
    assert exc.value.code == "invalid_input"
    errors = exc.value.details["errors"]
    assert any(e["field"] == "repeat" and e["problem"] == "type" for e in errors)


def test_validate_min_constraint() -> None:
    registry = load_registry(FIXTURE_ROOT)
    cap = registry.capability("echo.text")
    piece = {"text": "hello", "repeat": 0}
    with pytest.raises(FacetError) as exc:
        validate(cap, piece, Path.cwd())
    assert exc.value.code == "invalid_input"
    errors = exc.value.details["errors"]
    assert any(e["field"] == "repeat" and e["problem"] == "min" for e in errors)


def test_validate_max_constraint() -> None:
    registry = load_registry(FIXTURE_ROOT)
    cap = registry.capability("echo.text")
    piece = {"text": "hello", "repeat": 11}
    with pytest.raises(FacetError) as exc:
        validate(cap, piece, Path.cwd())
    assert exc.value.code == "invalid_input"
    errors = exc.value.details["errors"]
    assert any(e["field"] == "repeat" and e["problem"] == "max" for e in errors)


def test_validate_pattern_constraint() -> None:
    registry = load_registry(FIXTURE_ROOT)
    cap = registry.capability("echo.text")
    field_with_pattern = Field(
        name="test",
        type="string",
        pattern=r"^[a-z]+$",
    )
    cap_with_pattern = type(cap)(
        id=cap.id,
        package=cap.package,
        summary=cap.summary,
        kind=cap.kind,
        inputs=(field_with_pattern,),
        thresholds=cap.thresholds,
        viewer=cap.viewer,
    )
    with pytest.raises(FacetError) as exc:
        validate(cap_with_pattern, {"test": "ABC123"}, Path.cwd())
    assert exc.value.code == "invalid_input"
    errors = exc.value.details["errors"]
    assert any(e["field"] == "test" and e["problem"] == "pattern" for e in errors)


def test_validate_path_field_resolves() -> None:
    registry = load_registry(FIXTURE_ROOT)
    field = Field(name="file", type="path", required=True)
    cap = type(registry.capability("echo.text"))(
        id="test.path",
        package="test",
        summary="test",
        kind="text",
        inputs=(field,),
        thresholds={},
        viewer="generic",
    )
    with pytest.raises(FacetError) as exc:
        validate(cap, {"file": "nonexistent.txt"}, Path.cwd())
    assert exc.value.code == "invalid_input"
    errors = exc.value.details["errors"]
    assert any(e["field"] == "file" and e["problem"] == "not_found" for e in errors)


def test_validate_array_one_level() -> None:
    registry = load_registry(FIXTURE_ROOT)
    item_field = Field(name="text", type="string", required=True)
    array_field = Field(name="items", type="array", items=(item_field,), min_items=1)
    cap = type(registry.capability("echo.text"))(
        id="test.array",
        package="test",
        summary="test",
        kind="text",
        inputs=(array_field,),
        thresholds={},
        viewer="generic",
    )
    result = validate(cap, {"items": [{"text": "a"}, {"text": "b"}]}, Path.cwd())
    assert result == {"items": [{"text": "a"}, {"text": "b"}]}


def test_validate_array_min_items() -> None:
    registry = load_registry(FIXTURE_ROOT)
    item_field = Field(name="text", type="string", required=True)
    array_field = Field(name="items", type="array", items=(item_field,), min_items=2)
    cap = type(registry.capability("echo.text"))(
        id="test.array",
        package="test",
        summary="test",
        kind="text",
        inputs=(array_field,),
        thresholds={},
        viewer="generic",
    )
    with pytest.raises(FacetError) as exc:
        validate(cap, {"items": [{"text": "a"}]}, Path.cwd())
    assert exc.value.code == "invalid_input"
    errors = exc.value.details["errors"]
    assert any(e["field"] == "items" and e["problem"] == "min_items" for e in errors)


def test_validate_array_nested_error_path() -> None:
    registry = load_registry(FIXTURE_ROOT)
    item_field = Field(name="text", type="string", required=True)
    array_field = Field(name="items", type="array", items=(item_field,))
    cap = type(registry.capability("echo.text"))(
        id="test.array",
        package="test",
        summary="test",
        kind="text",
        inputs=(array_field,),
        thresholds={},
        viewer="generic",
    )
    with pytest.raises(FacetError) as exc:
        validate(cap, {"items": [{"text": "a"}, {"text": 123}]}, Path.cwd())
    assert exc.value.code == "invalid_input"
    errors = exc.value.details["errors"]
    assert any(e["field"] == "items[1].text" and e["problem"] == "type" for e in errors)


def test_to_json_schema_echo_text_matches_golden() -> None:
    registry = load_registry(FIXTURE_ROOT)
    cap = registry.capability("echo.text")
    schema = to_json_schema(cap)
    golden = json.loads((GOLDEN_ROOT / "echo-text.json").read_text(encoding="utf-8"))
    assert schema == golden


def test_to_json_schema_draft_version() -> None:
    registry = load_registry(FIXTURE_ROOT)
    cap = registry.capability("echo.text")
    schema = to_json_schema(cap)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_to_json_schema_additional_properties_false() -> None:
    registry = load_registry(FIXTURE_ROOT)
    cap = registry.capability("echo.text")
    schema = to_json_schema(cap)
    assert schema["additionalProperties"] is False


def test_to_json_schema_includes_defaults() -> None:
    registry = load_registry(FIXTURE_ROOT)
    cap = registry.capability("echo.text")
    schema = to_json_schema(cap)
    assert schema["properties"]["repeat"]["default"] == 1


def test_to_json_schema_includes_min_max() -> None:
    registry = load_registry(FIXTURE_ROOT)
    cap = registry.capability("echo.text")
    schema = to_json_schema(cap)
    assert schema["properties"]["repeat"]["minimum"] == 1
    assert schema["properties"]["repeat"]["maximum"] == REPEAT_MAX


def test_to_json_schema_required_list() -> None:
    registry = load_registry(FIXTURE_ROOT)
    cap = registry.capability("echo.text")
    schema = to_json_schema(cap)
    assert schema["required"] == ["text"]
