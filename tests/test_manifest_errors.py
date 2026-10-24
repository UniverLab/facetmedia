from pathlib import Path

import pytest

from facetmedia.registry import validate_registry

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

MODEL = """
[[models]]
id = "m1"
spdx = "MIT"
bytes = 10
url = "https://example.com/m1.bin"
sha256 = "{sha}"
path = "m1.bin"
""".replace("{sha}", "a" * 64)


def _with_top(line: str) -> str:
    """The base manifest with one top-level key, placed before its first table."""
    return BASE.replace("[engine]", f"{line}\n\n[engine]")


CAPABILITY = """
[capabilities.speech]
summary = "speaks"
kind = "audio"
"""

CASES = [
    pytest.param(
        BASE.replace('type = "python-env"', 'type = "docker"'),
        "engine.type",
        "value",
        id="engine-type",
    ),
    pytest.param(
        BASE.replace('python = "3.12"', 'python = "three"'), "engine.python", "value", id="python"
    ),
    pytest.param(
        BASE.replace('type = "cli"', 'type = "http"'),
        "transport.type",
        "value",
        id="transport-type",
    ),
    pytest.param(
        BASE.replace('facet = "text"', 'facet = "song"'),
        "facet",
        "value: one of voice, audio, image, video, cad, text, presentation",
        id="facet-unknown",
    ),
    pytest.param(
        BASE.replace('facet = "text"\n', ""),
        "facet",
        "required",
        id="facet-missing",
    ),
    pytest.param(
        BASE.replace('requires_facet = ">=0.1.0"', 'requires_facet = ""'),
        "requires_facet",
        "value",
        id="requires-facet-empty",
    ),
    pytest.param(
        BASE.replace('provider = "test"', 'provider = ""'),
        "provider",
        "value",
        id="provider-empty",
    ),
    pytest.param(
        BASE + MODEL.replace("a" * 64, ""), "models[0].sha256", "value", id="model-sha256-empty"
    ),
    pytest.param(
        BASE.replace("ram_gb = 1", 'ram_gb = "1"'), "requirements.ram_gb", "type", id="ram-type"
    ),
    pytest.param(_with_top('licenses = "MIT"'), "licenses", "type", id="licenses-type"),
    pytest.param(_with_top('licenses = ["MIT"]'), "licenses[0].subject", "type", id="license-item"),
    pytest.param(
        BASE + '\n[[licenses]]\nsubject = "pkg"\n',
        "licenses[0].spdx",
        "required",
        id="license-spdx",
    ),
    pytest.param(_with_top('models = "x"'), "models", "type", id="models-type"),
    pytest.param(_with_top('models = ["x"]'), "models[0].id", "type", id="model-item"),
    pytest.param(BASE + MODEL.replace('"m1"', '"M 1"'), "models[0].id", "value", id="model-id"),
    pytest.param(BASE + MODEL + MODEL, "models[1].id", "duplicate", id="model-duplicate"),
    pytest.param(
        BASE + MODEL.replace("bytes = 10", "bytes = 0"), "models[0].bytes", "value", id="bytes"
    ),
    pytest.param(
        BASE + MODEL.replace("https://", "http://"), "models[0].url", "value", id="model-url"
    ),
    pytest.param(
        BASE + MODEL.replace("a" * 64, "xyz"), "models[0].sha256", "value", id="model-sha256"
    ),
    pytest.param(
        BASE + MODEL.replace('"m1.bin"', '"/etc/m1.bin"'),
        "models[0].path",
        "value",
        id="model-path",
    ),
    pytest.param(
        BASE + MODEL.replace('url = "https://example.com/m1.bin"\n', ""),
        "models[0].url",
        "required",
        id="model-no-source",
    ),
    pytest.param(
        BASE
        + MODEL.replace(
            'url = "https://example.com/m1.bin"', 'hf_repo = "x/y"\nrevision = "short"'
        ),
        "models[0].revision",
        "value",
        id="model-revision",
    ),
    pytest.param(
        BASE + CAPABILITY.replace('kind = "audio"', 'kind = "song"'),
        "capabilities.speech.kind",
        "value",
        id="capability-kind",
    ),
    pytest.param(
        BASE + CAPABILITY.replace("[capabilities.speech]", "[capabilities.Speech]"),
        "capabilities.Speech",
        "value",
        id="capability-name",
    ),
    pytest.param(_with_top('capabilities = "x"'), "capabilities", "type", id="capabilities-type"),
    pytest.param(
        BASE + CAPABILITY + "extra = 1\n",
        "capabilities.speech.extra",
        "unknown",
        id="capability-unknown",
    ),
    pytest.param(
        BASE + CAPABILITY + '\n[capabilities.speech.inputs.v]\ntype = "float"\n',
        "capabilities.speech.inputs.v.type",
        "value",
        id="field-type",
    ),
    pytest.param(
        BASE + CAPABILITY + '\n[capabilities.speech.inputs.v]\ntype = "enum"\n',
        "capabilities.speech.inputs.v.values",
        "required",
        id="enum-values",
    ),
    pytest.param(
        BASE + CAPABILITY + '\n[capabilities.speech.inputs.v]\ntype = "array"\n',
        "capabilities.speech.inputs.v.items",
        "required",
        id="array-items",
    ),
    pytest.param(
        BASE
        + CAPABILITY
        + '\n[capabilities.speech.inputs.v]\ntype = "array"\ndefault = "x"\n'
        + '\n[capabilities.speech.inputs.v.items.x]\ntype = "string"\n',
        "capabilities.speech.inputs.v.default",
        "type",
        id="array-default-type",
    ),
    pytest.param(
        BASE + CAPABILITY + '\n[capabilities.speech.inputs.v]\ntype = "integer"\ndefault = "x"\n',
        "capabilities.speech.inputs.v.default",
        "type",
        id="default-type",
    ),
    pytest.param(
        BASE
        + CAPABILITY
        + '\n[capabilities.speech.inputs.v]\ntype = "enum"\nvalues = ["a"]\ndefault = "b"\n',
        "capabilities.speech.inputs.v.default",
        "value",
        id="default-enum",
    ),
    pytest.param(
        BASE + CAPABILITY + '\n[capabilities.speech.inputs.v]\ntype = "string"\npattern = "("\n',
        "capabilities.speech.inputs.v.pattern",
        "value",
        id="pattern",
    ),
    pytest.param(
        BASE
        + CAPABILITY
        + '\n[capabilities.speech.inputs.v]\ntype = "integer"\npattern = "\\\\d+"\n',
        "capabilities.speech.inputs.v.pattern",
        "value",
        id="pattern-non-text",
    ),
    pytest.param(
        BASE + CAPABILITY + '\n[capabilities.speech.inputs.v]\ntype = "string"\nmin = 1\n',
        "capabilities.speech.inputs.v.min",
        "value",
        id="min-non-numeric",
    ),
    pytest.param(
        BASE + CAPABILITY + '\n[capabilities.speech.inputs.v]\ntype = "string"\nvalues = ["a"]\n',
        "capabilities.speech.inputs.v.values",
        "value",
        id="values-non-enum",
    ),
    pytest.param(
        BASE + CAPABILITY + '\n[capabilities.speech.inputs.v]\ntype = "string"\nmin_items = 1\n',
        "capabilities.speech.inputs.v.min_items",
        "value",
        id="min-items-non-array",
    ),
    pytest.param(
        BASE.replace('entry = "runner.py"', 'entry = "../runner.py"'),
        "transport.entry",
        "value",
        id="entry-traversal",
    ),
    pytest.param(
        BASE.replace('entry = "runner.py"', 'entry = ""'),
        "transport.entry",
        "value",
        id="entry-empty",
    ),
    pytest.param(
        BASE.replace(
            'entry = "runner.py"',
            'entry = "runner.py"\ninvocation_template = ""',
        ),
        "transport.invocation_template",
        "value",
        id="template-empty",
    ),
    pytest.param(
        BASE + CAPABILITY + '\n[capabilities.speech.verify]\nthresholds = "x"\n',
        "capabilities.speech.verify.thresholds",
        "type",
        id="thresholds-type",
    ),
    pytest.param(
        BASE + CAPABILITY + '\n[capabilities.speech.preview]\nviewer = "Bad Name"\n',
        "capabilities.speech.preview.viewer",
        "value",
        id="viewer-name",
    ),
    pytest.param(
        BASE + CAPABILITY + "\n[capabilities.speech.preview]\nfoo = 1\n",
        "capabilities.speech.preview.foo",
        "unknown",
        id="preview-unknown",
    ),
]


def _problems(manifest: str, root: Path) -> set[tuple[str, str, str]]:
    (root / "index.toml").write_text('version = 1\n[[packages]]\nname = "pkg"\n', encoding="utf-8")
    pkg_dir = root / "packages" / "pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "package.toml").write_text(manifest, encoding="utf-8")
    for name in ("pyproject.toml", "uv.lock", "runner.py"):
        (pkg_dir / name).write_text("", encoding="utf-8")
    return {
        (problem["file"], problem["key"], problem["problem"]) for problem in validate_registry(root)
    }


@pytest.mark.parametrize(("manifest", "key", "problem"), CASES)
def test_manifest_problem_is_reported_with_file_and_key(
    manifest: str, key: str, problem: str, tmp_path: Path
) -> None:
    assert ("packages/pkg/package.toml", key, problem) in _problems(manifest, tmp_path)
