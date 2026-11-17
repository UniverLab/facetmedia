"""``facet run`` on an outward capability: dry run, yes, receipt, read-back.

The bluesky fixture never touches a network; it reports what a real package
would, so every guarantee in the spec is observable here: a dry run writes
nothing, ``--yes`` is the only thing that sends, a receipt refuses a second
send, and the verdict is what the network holds, not what was intended.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from facetmedia import record
from facetmedia import run as run_module
from facetmedia.cli import main
from facetmedia.post import parse_dry_run, sources

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "outward-registry"
EXIT_OK = 0
EXIT_INVALID_INPUT = 2
EXIT_RUNNER_FAILED = 9
EXIT_VERIFICATION_FAILED = 3
EXIT_CONFIRMATION_REQUIRED = 11
SECRET = "fixture-app-password"
DIGEST = f"sha256:{hashlib.sha256(b'pixels').hexdigest()}"


@pytest.fixture(scope="module")
def seed_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One home with bluesky installed, shared by every test in this module."""
    root = tmp_path_factory.mktemp("facet-home-outward-seed")
    previous = {name: os.environ.get(name) for name in ("FACET_HOME", "FACET_REGISTRY")}
    os.environ["FACET_HOME"] = str(root)
    os.environ["FACET_REGISTRY"] = str(FIXTURES)
    try:
        assert main(["pull", "bluesky", "--json"]) == 0
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    return root


@pytest.fixture
def facet_home(seed_home: Path, monkeypatch: pytest.MonkeyPatch, _facet_home_tmp: None) -> Path:
    """The shared home, with its registry and its stored credential."""
    monkeypatch.setenv("FACET_HOME", str(seed_home))
    monkeypatch.setenv("FACET_REGISTRY", str(FIXTURES))
    store = seed_home / "secrets" / "bluesky.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({"app-password": SECRET}), encoding="utf-8")
    monkeypatch.delenv("FIXTURE_POST_STATE", raising=False)
    monkeypatch.delenv("FIXTURE_READ_BACK", raising=False)
    monkeypatch.delenv("FIXTURE_DROP_MEDIA", raising=False)
    monkeypatch.delenv("FIXTURE_CALL_LOG", raising=False)
    monkeypatch.delenv("FIXTURE_BAD_RUN", raising=False)
    return seed_home


@pytest.fixture
def piece(tmp_path: Path) -> Path:
    """A piece and the one source asset it carries, both in ``tmp_path``."""
    (tmp_path / "card.png").write_bytes(b"pixels")
    piece = tmp_path / "talk.toml"
    piece.write_text(
        '[inputs]\ntext = "We shipped it."\nmedia = [{ image = "card.png" }]\n',
        encoding="utf-8",
    )
    return piece


def _envelope(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    parsed: Any = json.loads(capsys.readouterr().out)
    assert isinstance(parsed, dict)
    return parsed


def _receipt(path: Path) -> dict[str, Any]:
    parsed: Any = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def _send(piece: Path, out: Path, *extra: str) -> int:
    return main(["run", "bluesky.send", str(piece), "--out", str(out), *extra])


# --- the dry run sends nothing and writes nothing into the output folder.


def test_a_dry_run_previews_and_writes_nothing(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    facet_home: Path,
    piece: Path,
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    code = _send(piece, out, "--dry-run", "--json")
    envelope = _envelope(capsys)
    assert code == EXIT_OK
    assert envelope["data"]["dry_run"] is True
    assert "assets" not in envelope["data"]
    posts = envelope["data"]["preview"]["posts"]
    assert len(posts) == 1
    post = posts[0]
    assert post["text"] == "We shipped it."
    assert post["account"] == "lab.test"
    assert post["media"] == [str(tmp_path / "card.png")]
    assert post["warnings"] == []
    assert post["cost"] == {"amount": "0", "currency": "USD"}
    # Nothing landed in the output folder: no receipt and no record.
    assert list(out.iterdir()) == []


def test_a_dry_run_reports_the_networks_warnings(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    facet_home: Path,
    piece: Path,
) -> None:
    piece.write_text(
        '[inputs]\ntext = "' + "x" * 400 + '"\nmedia = [{ image = "card.png" }]\n',
        encoding="utf-8",
    )
    assert _send(piece, tmp_path, "--dry-run", "--json") == EXIT_OK
    post = _envelope(capsys)["data"]["preview"]["posts"][0]
    assert post["warnings"] == ["the text is over the network's 300 limit"]


def test_the_terminal_shows_the_dry_run_preview(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    facet_home: Path,
    piece: Path,
) -> None:
    code = _send(piece, tmp_path, "--dry-run")
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "dry run: 1 post(s), nothing sent" in out
    assert "lab.test" in out
    assert "We shipped it." in out
    assert "cost 0 USD" in out


def test_a_dry_run_calls_only_the_dry_run_verb(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    facet_home: Path,
    piece: Path,
) -> None:
    """A dry run asks what would be sent; the sending verb is never called."""
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FIXTURE_CALL_LOG", str(log))
    assert _send(piece, tmp_path, "--dry-run", "--json") == EXIT_OK
    capsys.readouterr()
    assert log.read_text(encoding="utf-8").splitlines() == ["dry-run"]


# --- the flags belong to the outward capability alone.


def test_dry_run_on_a_file_capability_is_invalid_input(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    facet_home: Path,
) -> None:
    """The flag belongs to an outward capability: a text one refuses it."""
    echo = tmp_path / "echo.toml"
    echo.write_text('[inputs]\ntext = "hi"\n', encoding="utf-8")
    monkeypatch.setenv(
        "FACET_REGISTRY", str(Path(__file__).resolve().parent / "fixtures" / "registry")
    )
    code = main(["run", "echo.text", str(echo), "--dry-run", "--json"])
    error = _envelope(capsys)["error"]
    assert code == EXIT_INVALID_INPUT
    assert error["code"] == "invalid_input"
    assert "--dry-run is only for an outward capability" in error["message"]


def test_no_verify_on_an_outward_capability_is_invalid_input(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    facet_home: Path,
    piece: Path,
) -> None:
    code = _send(piece, tmp_path, "--no-verify", "--yes", "--json")
    error = _envelope(capsys)["error"]
    assert code == EXIT_INVALID_INPUT
    assert error["code"] == "invalid_input"
    assert "--no-verify is not allowed for an outward capability" in error["message"]


def test_yes_on_a_file_capability_is_invalid_input(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    facet_home: Path,
) -> None:
    """``--yes`` means "send without asking": a file capability has nothing to send."""
    echo = tmp_path / "echo.toml"
    echo.write_text('[inputs]\ntext = "hi"\n', encoding="utf-8")
    monkeypatch.setenv(
        "FACET_REGISTRY", str(Path(__file__).resolve().parent / "fixtures" / "registry")
    )
    code = main(["run", "echo.text", str(echo), "--yes", "--json"])
    error = _envelope(capsys)["error"]
    assert code == EXIT_INVALID_INPUT
    assert error["code"] == "invalid_input"
    assert "--yes is only for an outward capability" in error["message"]


# --- an explicit yes, and only that.


def test_a_real_run_without_yes_and_a_terminal_is_not_sent(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    facet_home: Path,
    piece: Path,
) -> None:
    """Off a terminal there is nobody to ask: exit 11 with the preview."""
    out = tmp_path / "out"
    out.mkdir()
    code = _send(piece, out, "--json")
    error = _envelope(capsys)["error"]
    assert code == EXIT_CONFIRMATION_REQUIRED
    assert error["code"] == "confirmation_required"
    assert error["hint"] == "add --yes once the preview is what you meant to send"
    assert error["details"]["preview"]["posts"][0]["text"] == "We shipped it."
    assert list(out.iterdir()) == []


def test_a_declined_confirmation_sends_nothing_and_exits_0(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    facet_home: Path,
    piece: Path,
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(run_module.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "n")
    assert _send(piece, out) == EXIT_OK
    captured = capsys.readouterr()
    assert "dry run: 1 post(s), nothing sent" in captured.out
    assert "nothing sent" in captured.out
    assert list(out.iterdir()) == []


def test_a_confirmed_terminal_run_sends_once(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    facet_home: Path,
    piece: Path,
) -> None:
    """Typing ``y`` at the prompt is the other way to an explicit yes."""
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(run_module.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")
    assert _send(piece, out) == EXIT_OK
    captured = capsys.readouterr()
    assert "nothing sent" not in [line.strip() for line in captured.out.splitlines()]
    receipt = _receipt(out / "talk.post.json")
    assert receipt["state"] == "published"
    assert "talk.post.json talk.post passed" in captured.out


def test_a_confirmed_run_sends_once_and_writes_the_receipt(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    facet_home: Path,
    piece: Path,
) -> None:
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FIXTURE_CALL_LOG", str(log))
    out = tmp_path / "out"
    code = _send(piece, out, "--yes", "--json")
    envelope = _envelope(capsys)
    assert code == EXIT_OK
    assert envelope["data"]["sent"] is True
    # One preview, one send, one read-back: facet never retries a send.
    assert log.read_text(encoding="utf-8").splitlines() == ["dry-run", "run", "verify"]
    assets = envelope["data"]["assets"]
    assert len(assets) == 1
    assert assets[0]["kind"] == "post"
    assert assets[0]["path"] == "talk.post.json"

    receipt = _receipt(out / "talk.post.json")
    assert receipt["network"] == "bluesky"
    assert receipt["account"] == "lab.test"
    assert receipt["state"] == "published"
    assert receipt["cost"] == {"amount": "0", "currency": "USD"}
    assert receipt["posts"][0]["url"].startswith("https://bsky.app/profile/lab.test/post/")
    assert receipt["sent_at"].endswith("Z")
    assert receipt["verified_at"].endswith("Z")
    assert len(receipt["sources"]) == 1
    carried = receipt["sources"][0]
    assert carried["id"] == DIGEST
    assert carried["generated"] is False

    stored = json.loads((out / "talk.post.json.facet.json").read_text(encoding="utf-8"))
    assert stored["verification"] == {
        "status": "passed",
        "metrics": {"state": "published", "media": 1},
        "thresholds": {},
        "verifier": "post-read-back",
    }
    provenance = stored["provenance"]
    assert provenance["capability"] == "bluesky.send"
    assert provenance["inputs"][0]["id"] == DIGEST
    assert record.verify_integrity(out / "talk.post.json") is True


def test_the_receipt_is_named_after_the_piece_not_the_run(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    facet_home: Path,
) -> None:
    (tmp_path / "card.png").write_bytes(b"pixels")
    piece = tmp_path / "announcement.toml"
    piece.write_text('[inputs]\ntext = "hi"\nmedia = [{ image = "card.png" }]\n', encoding="utf-8")
    assert _send(piece, tmp_path, "--yes", "--json") == EXIT_OK
    envelope = _envelope(capsys)
    assert (tmp_path / "announcement.post.json").is_file()
    assert envelope["data"]["assets"][0]["path"] == "announcement.post.json"


# --- the receipt-exists rule.


def test_a_second_real_run_refuses_the_existing_receipt(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    facet_home: Path,
    piece: Path,
) -> None:
    out = tmp_path / "out"
    assert _send(piece, out, "--yes", "--json") == EXIT_OK
    capsys.readouterr()
    marker = (out / "talk.post.json").read_bytes()

    code = _send(piece, out, "--yes", "--json")
    error = _envelope(capsys)["error"]
    assert code == EXIT_INVALID_INPUT
    assert error["code"] == "invalid_input"
    assert error["message"] == f"{out / 'talk.post.json'}: this piece was already sent"
    assert error["details"] == {"receipt": str(out / "talk.post.json")}
    assert "remove that receipt" in error["hint"]
    assert "--out" in error["hint"]
    # The first receipt is untouched: the refusal happened before any send.
    assert (out / "talk.post.json").read_bytes() == marker


def test_a_dry_run_with_a_receipt_still_previews_and_warns(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    facet_home: Path,
    piece: Path,
) -> None:
    out = tmp_path / "out"
    assert _send(piece, out, "--yes", "--json") == EXIT_OK
    capsys.readouterr()

    assert _send(piece, out, "--dry-run", "--json") == EXIT_OK
    preview = _envelope(capsys)["data"]["preview"]
    assert preview["posts"][0]["text"] == "We shipped it."
    assert any("receipt already exists" in warning for warning in preview["warnings"])


# --- the receipt is honest about what the network holds.


def test_a_draft_read_back_fails_and_seals_the_receipt_unknown(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    facet_home: Path,
    piece: Path,
) -> None:
    monkeypatch.setenv("FIXTURE_READ_BACK", "draft")
    out = tmp_path / "out"
    code = _send(piece, out, "--yes", "--json")
    error = _envelope(capsys)["error"]
    assert code == EXIT_VERIFICATION_FAILED
    assert error["code"] == "verification_failed"
    assert "the network holds it as draft" in error["message"]
    assert error["hint"].startswith("check the post at https://bsky.app/")
    assert error["details"]["state"] == "draft"

    receipt = _receipt(out / "talk.post.json")
    assert receipt["state"] == "unknown"
    assert receipt["verified_at"] is None
    stored = json.loads((out / "talk.post.json.facet.json").read_text(encoding="utf-8"))
    assert stored["verification"]["status"] == "failed"
    assert stored["verification"]["metrics"]["state"] == "draft"


def test_a_read_back_that_cannot_happen_says_the_post_may_exist(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    facet_home: Path,
    piece: Path,
) -> None:
    """A verify verb that breaks the protocol keeps the receipt, sealed unknown."""
    target = facet_home / "envs" / "bluesky" / "0.1.0" / "package" / "runner.py"
    original = target.read_text(encoding="utf-8")
    broken = original.replace(
        'return {"state": state, "media": media}',
        'raise RuntimeError("the network is unreachable")',
    )
    assert broken != original
    target.write_text(broken, encoding="utf-8")
    try:
        out = tmp_path / "out"
        code = _send(piece, out, "--yes", "--json")
        error = _envelope(capsys)["error"]
    finally:
        target.write_text(original, encoding="utf-8")
    assert code == EXIT_RUNNER_FAILED
    assert error["code"] == "runner_failed"
    assert "the post may exist" in error["message"]
    assert error["details"]["receipt"] == str(out / "talk.post.json")
    assert error["details"]["urls"]
    receipt = _receipt(out / "talk.post.json")
    assert receipt["state"] == "unknown"
    assert receipt["verified_at"] is None


def test_a_send_whose_answer_breaks_the_protocol_keeps_an_unknown_receipt(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    facet_home: Path,
    piece: Path,
) -> None:
    """The verb exited 0, so a request may have left: the receipt still exists."""
    monkeypatch.setenv("FIXTURE_BAD_RUN", "1")
    out = tmp_path / "out"
    code = _send(piece, out, "--yes", "--json")
    error = _envelope(capsys)["error"]
    assert code == EXIT_RUNNER_FAILED
    assert error["code"] == "runner_failed"
    assert "the post may exist" in error["message"]
    receipt = _receipt(out / "talk.post.json")
    assert receipt["state"] == "unknown"
    assert receipt["account"] == "lab.test"
    assert receipt["posts"] == []
    # The receipt-exists rule now refuses a second send over the same output.
    assert _send(piece, out, "--yes", "--json") == EXIT_INVALID_INPUT
    assert _envelope(capsys)["error"]["message"].endswith("this piece was already sent")


# --- `facet check` reads a post back, never judging it by its bytes alone.


def test_check_reads_a_post_back_and_passes_when_the_network_agrees(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    facet_home: Path,
    piece: Path,
) -> None:
    out = tmp_path / "out"
    assert _send(piece, out, "--yes", "--json") == EXIT_OK
    capsys.readouterr()
    code = main(["check", str(out / "talk.post.json"), "--json"])
    data = _envelope(capsys)["data"]
    assert code == EXIT_OK
    assert data["passed"] is True
    assert data["fresh"]["verifier"] == "post-read-back"
    assert data["fresh"]["metrics"] == {"state": "published", "media": 1}


def test_check_fails_a_post_the_network_now_holds_as_a_draft(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    facet_home: Path,
    piece: Path,
) -> None:
    out = tmp_path / "out"
    assert _send(piece, out, "--yes", "--json") == EXIT_OK
    capsys.readouterr()
    monkeypatch.setenv("FIXTURE_READ_BACK", "draft")
    code = main(["check", str(out / "talk.post.json"), "--json"])
    data = _envelope(capsys)["data"]
    assert code == EXIT_VERIFICATION_FAILED
    assert data["passed"] is False
    assert data["fresh"]["metrics"]["state"] == "draft"
    # The receipt is never modified by a check.
    assert _receipt(out / "talk.post.json")["state"] == "published"


def test_check_fails_a_post_whose_media_count_differs(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    facet_home: Path,
    piece: Path,
) -> None:
    """The state alone is not enough: the network must list the media sent."""
    out = tmp_path / "out"
    assert _send(piece, out, "--yes", "--json") == EXIT_OK
    capsys.readouterr()
    monkeypatch.setenv("FIXTURE_DROP_MEDIA", "1")
    code = main(["check", str(out / "talk.post.json"), "--json"])
    data = _envelope(capsys)["data"]
    assert code == EXIT_VERIFICATION_FAILED
    assert data["passed"] is False
    assert data["fresh"]["metrics"] == {"state": "published", "media": 0}


# --- the source assets and their generated flag.


def test_a_source_is_generated_only_when_its_record_names_a_model(tmp_path: Path) -> None:
    """``sources`` derives ``generated`` from the record, never from a guessing agent."""
    made = tmp_path / "made.png"
    made.write_bytes(b"made")
    plain = tmp_path / "plain.png"
    plain.write_bytes(b"plain")
    _record(made, models=[{"id": "a-model", "bytes": 1}])
    _record(plain, models=[])
    carried = sources((made, plain, tmp_path / "missing.png"), {})
    assert [(entry["generated"], entry["id"]) for entry in carried] == [
        (True, f"sha256:{hashlib.sha256(b'made').hexdigest()}"),
        (False, f"sha256:{hashlib.sha256(b'plain').hexdigest()}"),
        (False, None),
    ]


def _record(path: Path, *, models: list[dict[str, object]]) -> None:
    """A minimal record beside ``path``; only ``provenance.models`` matters here."""
    payload = {
        "record": 1,
        "id": f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
        "name": path.stem,
        "path": path.name,
        "kind": "image",
        "mime": "image/png",
        "bytes": path.stat().st_size,
        "properties": {},
        "provenance": {
            "capability": "echo.text",
            "package": {"name": "echo", "version": "0.1.0"},
            "models": models,
        },
        "verification": {"status": "passed"},
    }
    path.with_name(f"{path.name}.facet.json").write_text(json.dumps(payload), encoding="utf-8")


def test_a_dry_run_carries_the_runners_group_warnings(tmp_path: Path) -> None:
    """A warning about the whole send is not lost on the way to the envelope."""
    preview = parse_dry_run({"posts": [], "warnings": ["the link raises the cost"]}, tmp_path)
    assert preview == {"posts": [], "warnings": ["the link raises the cost"]}
