"""Progress lines and the verbose working log of ``facet pull``.

The TTY cases drive a fake stream and assert the final line, never every
frame; the plain cases assert the start and end lines a pipe receives; the
failure cases assert the log tail and the log path.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from facetmedia import env as env_module
from facetmedia import progress
from facetmedia.cli import build_parser, main
from facetmedia.home import Home, log_file
from facetmedia.progress import Progress, announce_failure, set_verbose
from facetmedia.registry import load_registry

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry"
EXIT_LICENSE_NOT_ACCEPTED = 4
EXIT_DOWNLOAD_FAILED = 8


class FakeClock:
    """A monotonic clock that advances one step per call."""

    def __init__(self, step: float = 0.25) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


class FakeTty(io.StringIO):
    """A stream that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _loud() -> None:
    """Every test starts and ends with the working log echoed."""
    set_verbose(True)


def _registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACET_REGISTRY", str(FIXTURES))


# --- the TTY line.


def test_tty_bar_ends_with_a_check_and_the_elapsed_time() -> None:
    stream = io.StringIO()
    reporter = Progress("tty", stream, FakeClock())

    reporter.phase_start("models", total=100)
    reporter.advance(50)
    reporter.advance(50)
    reporter.phase_end(ok=True)

    out = stream.getvalue()
    assert "\r" in out
    assert "[█" in out, "a known total renders a bar"
    assert "MB/s" in out
    assert out.rstrip().endswith("s)"), "the last line carries the elapsed time"
    assert "✓ models (" in out.split("\r")[-1]


def test_tty_spinner_runs_until_the_phase_ends() -> None:
    stream = io.StringIO()
    reporter = Progress("tty", stream, FakeClock())

    reporter.phase_start("environment")
    reporter.advance(1024)
    reporter.phase_end(ok=True)

    out = stream.getvalue()
    assert any(frame in out for frame in progress.SPINNER)
    assert "✓ environment (" in out.split("\r")[-1]


def test_tty_failure_names_the_one_line_error() -> None:
    stream = io.StringIO()
    reporter = Progress("tty", stream, FakeClock())

    reporter.phase_start("environment")
    reporter.phase_end(ok=False, err="uv sync failed")

    assert "✗ environment: uv sync failed\n" in stream.getvalue()


def test_a_late_content_length_turns_the_spinner_into_a_bar() -> None:
    stream = io.StringIO()
    reporter = Progress("tty", stream, FakeClock())

    reporter.phase_start("artifacts")
    reporter.advance(1024)
    reporter.phase_total(4096)
    reporter.advance(1024)
    reporter.phase_end(ok=True)

    out = stream.getvalue()
    assert "MB/s" in out, "the artifact bar only appears once its size is known"
    assert "[█" in out


# --- the plain line of a pipe or a CI log.


def test_plain_mode_prints_one_line_per_phase_start_and_end() -> None:
    stream = io.StringIO()
    reporter = Progress("plain", stream)

    reporter.phase_start("models", total=10)
    reporter.advance(10)
    reporter.phase_end(ok=True)

    out = stream.getvalue()
    assert out.startswith("start models\n")
    assert out.splitlines()[-1] == "end ✓ models (0.0s)"
    assert "\r" not in out


def test_plain_mode_reports_a_failure_on_the_end_line() -> None:
    stream = io.StringIO()
    reporter = Progress("plain", stream)

    reporter.phase_start("artifacts")
    reporter.phase_end(ok=False, err="HTTP 404")

    assert stream.getvalue().splitlines()[-1] == "end ✗ artifacts: HTTP 404"


def test_off_mode_draws_nothing() -> None:
    stream = io.StringIO()
    reporter = Progress("off", stream)

    reporter.phase_start("models", total=10)
    reporter.advance(5)
    reporter.phase_end(ok=True)

    assert stream.getvalue() == ""


def test_for_cli_picks_the_mode() -> None:
    tty = FakeTty()
    assert Progress.for_cli(as_json=False, verbose=False, stream=tty).mode == "tty"
    assert Progress.for_cli(as_json=False, verbose=False, stream=io.StringIO()).mode == "plain"
    assert Progress.for_cli(as_json=True, verbose=False, stream=tty).mode == "off"
    assert Progress.for_cli(as_json=False, verbose=True, stream=tty).mode == "off"


# --- the detailed working log is verbose only.


def test_the_plan_is_written_to_the_log_and_shown_when_verbose(
    capsys: pytest.CaptureFixture[str],
) -> None:
    package = load_registry(FIXTURES).packages["echo"]
    home = Home.from_env()
    env_dir = home.env_dir(package.name, package.version)

    set_verbose(False)
    env_module._print_plan(package, home, env_dir, False)
    assert capsys.readouterr().err == ""

    set_verbose(True)
    env_module._print_plan(package, home, env_dir, False)
    assert "python 3.12" in capsys.readouterr().err


def test_uv_output_is_echoed_only_when_verbose(capsys: pytest.CaptureFixture[str]) -> None:
    argv = [sys.executable, "-c", "print('resolved 3 packages')"]

    set_verbose(False)
    env_module._stream_output(argv, {})
    assert "resolved 3 packages" not in capsys.readouterr().err

    set_verbose(True)
    env_module._stream_output(argv, {})
    assert "resolved 3 packages" in capsys.readouterr().err


def test_pull_draws_one_line_per_phase_and_keeps_the_plan_in_the_log(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    package = load_registry(FIXTURES).packages["echo"]
    home = Home.from_env()
    monkeypatch.setattr(env_module, "_preflight", lambda *args, **kwargs: None)
    monkeypatch.setattr(env_module, "_rebuild", lambda *args, **kwargs: None)
    monkeypatch.setattr(env_module, "_fetch_server", lambda *args, **kwargs: None)
    monkeypatch.setattr(env_module, "fetch_source", lambda *args, **kwargs: None)
    monkeypatch.setattr(env_module, "_fetch_models", lambda *args, **kwargs: ([], 0))
    monkeypatch.setattr(env_module, "_fetch_artifacts", lambda *args, **kwargs: 0)
    stream = io.StringIO()
    set_verbose(False)

    report = env_module.pull(package, home, progress=Progress("plain", stream))

    out = stream.getvalue()
    assert "start environment" in out
    assert "end ✓ environment" in out
    assert "end ✓ models" in out
    assert "end ✓ artifacts" in out
    assert report["downloaded_bytes"] == 0
    assert capsys.readouterr().err == "", "the plan stayed in the log, not on stderr"


def test_pull_reports_a_failed_phase_with_its_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = load_registry(FIXTURES).packages["echo"]
    home = Home.from_env()
    monkeypatch.setattr(env_module, "_preflight", lambda *args, **kwargs: None)

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(env_module, "_rebuild", boom)
    stream = io.StringIO()

    with pytest.raises(OSError):
        env_module.pull(package, home, progress=Progress("plain", stream))

    assert "end ✗ environment: disk full" in stream.getvalue()


# --- the failure tail and the log path.


def test_announce_failure_repeats_the_last_twenty_lines_and_the_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = "20260101T000000Z-abcdef"
    path = log_file(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"line {index}\n" for index in range(30)), encoding="utf-8")

    announce_failure(run_id)

    lines = capsys.readouterr().err.splitlines()
    assert lines[0] == "line 10"
    assert lines[-2] == "line 29"
    assert lines[-1] == f"log: {path}"


def test_pull_failure_prints_the_log_tail_even_without_verbose(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)

    code = main(["pull", "termsy"])

    captured = capsys.readouterr()
    assert code == EXIT_LICENSE_NOT_ACCEPTED
    lines = captured.err.splitlines()
    log_index = next(index for index, line in enumerate(lines) if line.startswith("log: "))
    assert log_index <= progress.TAIL_LINES
    path = Path(lines[log_index].removeprefix("log: "))
    assert path.is_file()
    assert "license_not_accepted" in captured.err or "accept" in captured.err


def test_pull_json_keeps_its_envelope_and_draws_no_progress_line(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)

    code = main(["pull", "termsy", "--json"])

    captured = capsys.readouterr()
    assert code == EXIT_LICENSE_NOT_ACCEPTED
    envelope = json.loads(captured.out)
    assert envelope["error"]["code"] == "license_not_accepted"
    assert "start " not in captured.err
    assert "end " not in captured.err


def test_pull_verbose_echoes_the_plan_instead_of_a_bar(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    # The failing uv stops the pull after the plan, so no environment is built.
    monkeypatch.setattr(env_module.uv, "find_uv_bin", lambda: "/bin/false")

    code = main(["pull", "echo", "--verbose"])

    err = capsys.readouterr().err
    assert code == EXIT_DOWNLOAD_FAILED
    assert "total to download" in err
    assert "start " not in err, "verbose shows the log, not the bars"


def test_verbose_flags_parse() -> None:
    parser = build_parser()
    assert parser.parse_args(["pull", "x", "-v"]).verbose is True
    assert parser.parse_args(["pull", "x", "--verbose"]).verbose is True
    assert parser.parse_args(["update", "-v"]).verbose is True
    assert parser.parse_args(["pull", "x"]).verbose is False


def test_main_restores_the_default_verbosity(monkeypatch: pytest.MonkeyPatch) -> None:
    _registry(monkeypatch)

    main(["pull", "termsy"])

    assert progress.verbose() is True
