#!/usr/bin/env bash
# The v0.1 success metric, measured and not assumed: on a machine with only
# `uv`, one command produces a verified voice clip (WER < 5 %) in under five
# minutes without a GPU, and the other v0.1 packages still pass their examples.
#
# Everything facet or uv writes goes under one temporary root: HOME, the uv
# caches, the managed Python and FACET_HOME. The host's Python, models and
# wheels are unreachable, so a green run is a clean-machine run.
#
# The packages come from UniverLab/facetmedia-registry through `facet update`,
# exactly as a user gets them: the first use fetches the registry by itself, and
# that download is inside the timed part.
set -euo pipefail

INVOCATION_DIR=$PWD
REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$REPO_ROOT"

#: The voice run must finish within this; the spec's number, in milliseconds.
BUDGET_MS=$((300 * 1000))

#: feature/v0.1 until the registry's v0.1 is merged, then main.
REGISTRY_REF=feature/v0.1
WHEEL=""
ROOT=""
KEEP=0

#: Every package of v0.1, copied from the registry snapshot in this order.
PACKAGES=(whisper ffmpeg cad ascii)

#: package, capability, piece: the registry's own examples, run one at a time.
EXAMPLES=(
  "whisper whisper.transcribe laboratorio"
  "ffmpeg ffmpeg.normalize normalize"
  "ffmpeg ffmpeg.mix mix"
  "ffmpeg ffmpeg.convert convert"
  "cad cad.solid escuadra"
  "cad cad.drawing plano-estudio"
  "ascii ascii.banner banner-canopy"
  "ascii ascii.image lamina"
)

SUMMARY=()
STEP_LABEL=""
STEP_T0=0
TOTAL_T0=0
TIMED_MS=0
ROOT_CREATED=0
FACET=""
SNAP=""

absolute() {
  case $1 in
    /*) printf '%s' "$1" ;;
    *) printf '%s/%s' "$INVOCATION_DIR" "$1" ;;
  esac
}

usage() {
  cat <<'USAGE'
usage: scripts/acceptance.sh [--wheel PATH] [--registry-ref REF] [--root DIR] [--keep]

  --wheel PATH          install this wheel instead of building one
  --registry-ref REF    registry ref to fetch (default: feature/v0.1)
  --root DIR            use DIR as the clean root instead of a fresh mktemp -d
  --keep                keep the root (assets, logs and the kokoro envelope)
USAGE
}

die() {
  printf 'FAIL: %s\n' "$*" >&2
  print_summary >&2
  exit 1
}

seconds() {
  printf '%d.%01d s' $(($1 / 1000)) $((($1 % 1000) / 100))
}

summary_line() {
  printf '%-48s %s' "$1" "$2"
}

gib() {
  local bytes=$1 bound=1073741824
  printf '%d.%01d GiB' $((bytes / bound)) $(((bytes % bound) * 10 / bound))
}

parse_args() {
  while [ $# -gt 0 ]; do
    case $1 in
      --wheel)
        WHEEL=${2:?--wheel needs a path}
        shift 2
        ;;
      --wheel=*)
        WHEEL=${1#*=}
        shift
        ;;
      --registry-ref)
        REGISTRY_REF=${2:?--registry-ref needs a ref}
        shift 2
        ;;
      --registry-ref=*)
        REGISTRY_REF=${1#*=}
        shift
        ;;
      --root)
        ROOT=${2:?--root needs a directory}
        shift 2
        ;;
      --root=*)
        ROOT=${1#*=}
        shift
        ;;
      --keep)
        KEEP=1
        shift
        ;;
      -h | --help)
        usage
        exit 0
        ;;
      *)
        usage >&2
        die "unknown argument: $1"
        ;;
    esac
  done
}

prepare_root() {
  command -v uv >/dev/null || die "uv is not on PATH; the metric starts with only uv installed"
  command -v python3 >/dev/null || die "python3 is not on PATH; the assertions need it"
  TOTAL_T0=$(date +%s%N)
  if [ -z "$ROOT" ]; then
    ROOT=$(mktemp -d "${TMPDIR:-/tmp}/facet-acceptance.XXXXXX")
  else
    ROOT=$(absolute "$ROOT")
    mkdir -p "$ROOT"
    ROOT=$(cd "$ROOT" && pwd)
  fi
  export HOME="$ROOT/home"
  export FACET_HOME="$ROOT/facet"
  export UV_CACHE_DIR="$ROOT/uv-cache"
  export UV_TOOL_DIR="$ROOT/uv-tools"
  export UV_TOOL_BIN_DIR="$ROOT/uv-bin"
  export UV_PYTHON_INSTALL_DIR="$ROOT/uv-python"
  export XDG_CACHE_HOME="$ROOT/xdg-cache"
  export XDG_DATA_HOME="$ROOT/xdg-data"
  export FACET_REGISTRY_REF="$REGISTRY_REF"
  # The GPU is not part of the metric, and neither is any cache or Python the
  # host keeps. GITHUB_TOKEN, when the caller set it, is left untouched.
  export CUDA_VISIBLE_DEVICES=""
  unset FACET_REGISTRY
  unset VIRTUAL_ENV PYTHONPATH PYTHONHOME
  mkdir -p "$HOME" "$FACET_HOME" "$UV_CACHE_DIR" "$UV_TOOL_DIR" "$UV_TOOL_BIN_DIR" \
    "$UV_PYTHON_INSTALL_DIR" "$XDG_CACHE_HOME" "$XDG_DATA_HOME" \
    "$ROOT/out" "$ROOT/envelopes" "$ROOT/hashes"
  FACET="$UV_TOOL_BIN_DIR/facet"
  ROOT_CREATED=1
  printf 'clean root: %s\n' "$ROOT"
  printf 'registry ref: %s\n' "$REGISTRY_REF"
}

step_begin() {
  STEP_LABEL=$1
  STEP_T0=$(date +%s%N)
  printf '== %s\n' "$STEP_LABEL"
}

step_end() {
  local ms=$((($(date +%s%N) - STEP_T0) / 1000000))
  SUMMARY+=("$(summary_line "$STEP_LABEL" "$(seconds "$ms")")")
}

build_wheel() {
  if [ -n "$WHEEL" ]; then
    WHEEL=$(absolute "$WHEEL")
    [ -f "$WHEEL" ] || die "--wheel $WHEEL: no such file"
    SUMMARY+=("$(summary_line "wheel" "$(basename "$WHEEL") (given)")")
    return
  fi
  step_begin "build wheel"
  uv build
  WHEEL=$(ls -t dist/*.whl | awk 'NR == 1') || WHEEL=""
  [ -n "$WHEEL" ] || die "uv build wrote no wheel into dist/"
  WHEEL=$(cd "$(dirname "$WHEEL")" && pwd)/$(basename "$WHEEL")
  step_end
  SUMMARY+=("$(summary_line "wheel" "$(basename "$WHEEL")")")
}

# The timed part: install the wheel, then one command, no GPU, from a clean home.
install_and_run() {
  step_begin "install + voice run (timed)"
  local rc=0
  uv tool install "$WHEEL"
  [ -x "$FACET" ] || die "uv tool install left no facet at $FACET"
  "$FACET" run kokoro.speak tests/acceptance/voz.toml --out "$ROOT/out" --json \
    >"$ROOT/kokoro.json" || rc=$?
  TIMED_MS=$((($(date +%s%N) - STEP_T0) / 1000000))
  step_end
  if [ "$rc" -ne 0 ] || ! assert_kokoro "$ROOT/kokoro.json"; then
    show_failure "$ROOT/kokoro.json" "kokoro.speak"
    die "the voice run did not meet the v0.1 metric (exit $rc, $(seconds "$TIMED_MS"))"
  fi
  printf '  budget: %s of 300 s\n' "$(seconds "$TIMED_MS")"
}

assert_kokoro() {
  python3 -c '
import json
import sys

envelope = json.load(open(sys.argv[1], encoding="utf-8"))
elapsed_ms = int(sys.argv[2])
budget_ms = int(sys.argv[3])
problems = []
if envelope.get("ok") is not True:
    problems.append("ok is not true")
assets = (envelope.get("data") or {}).get("assets") or []
if len(assets) != 1:
    problems.append("expected one asset, got %d" % len(assets))
else:
    verification = assets[0].get("verification") or {}
    if verification.get("status") != "passed":
        problems.append("verification: %s" % verification.get("status"))
    wer = (verification.get("metrics") or {}).get("wer")
    if not isinstance(wer, (int, float)) or wer >= 0.05:
        problems.append("wer: %r (the metric needs < 0.05)" % (wer,))
if elapsed_ms >= budget_ms:
    problems.append("took %s s, the budget is %s s" % (elapsed_ms / 1000.0, budget_ms / 1000.0))
if problems:
    print("\n".join(problems))
    sys.exit(1)
' "$1" "$TIMED_MS" "$BUDGET_MS"
}

newest_log() {
  local path=""
  path=$(find "$FACET_HOME/logs" -type f -name '*.log' -printf '%T@ %p\n' 2>/dev/null |
    sort -nr | awk 'NR == 1 { sub(/^[^ ]+ /, ""); print }') || path=""
  printf '%s' "$path"
}

show_failure() {
  local envelope=$1 label=$2 log
  log=$(newest_log)
  printf '\n--- %s (%s) ---\n' "$envelope" "$label" >&2
  cat "$envelope" >&2 || true
  if [ -n "$log" ]; then
    printf '\n--- last 60 lines of %s ---\n' "$log" >&2
    tail -n 60 "$log" >&2 || true
  else
    printf '\n--- no run log under %s ---\n' "$FACET_HOME/logs" >&2
  fi
  return 0
}

newest_snapshot() {
  local path
  path=$(ls -dt "$FACET_HOME"/registry/*/ 2>/dev/null |
    awk 'NR == 1 { sub(/\/$/, ""); print }') || true
  printf '%s' "$path"
}

copy_examples() {
  local pkg=$1
  mkdir -p "$ROOT/$pkg"
  cp -R "$SNAP/packages/$pkg/examples/." "$ROOT/$pkg/"
}

hash_inputs() {
  (cd "$1" && find . -path ./out -prune -o -type f -print0 | sort -z | xargs -0 -r sha256sum)
}

assets_of() {
  python3 -c '
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))["data"]
for asset in data["assets"]:
    print(asset["path"])
' "$1"
}

run_examples() {
  step_begin "registry examples (whisper, ffmpeg, cad, ascii)"
  local pkg entry cap piece
  SNAP=$(newest_snapshot)
  [ -n "$SNAP" ] || die "no registry snapshot under $FACET_HOME/registry"
  SUMMARY+=("$(summary_line "registry snapshot" "$(basename "$SNAP")")")
  for pkg in "${PACKAGES[@]}"; do
    copy_examples "$pkg"
  done
  for entry in "${EXAMPLES[@]}"; do
    read -r pkg cap piece <<<"$entry"
    run_example "$pkg" "$cap" "$piece"
  done
  step_end
}

run_example() {
  local pkg=$1 cap=$2 piece=$3
  local dir="$ROOT/$pkg" out="$ROOT/$pkg/out"
  local envelope="$ROOT/envelopes/$pkg-$cap.json"
  local before="$ROOT/hashes/$pkg-$cap.before"
  local after="$ROOT/hashes/$pkg-$cap.after"
  local rc=0
  printf '  %s %s\n' "$cap" "$piece"
  mkdir -p "$out"
  hash_inputs "$dir" >"$before"
  "$FACET" run "$cap" "$dir/$piece.toml" --out "$out" --json >"$envelope" || rc=$?
  if [ "$rc" -ne 0 ] || ! assert_example "$envelope"; then
    show_failure "$envelope" "$cap"
    die "$cap $piece did not pass"
  fi
  hash_inputs "$dir" >"$after"
  cmp -s "$before" "$after" || die "$cap $piece changed one of its inputs"
  preview_assets "$pkg" "$envelope" "$out"
}

assert_example() {
  python3 -c '
import json
import sys

envelope = json.load(open(sys.argv[1], encoding="utf-8"))
problems = []
if envelope.get("ok") is not True:
    problems.append("ok is not true")
assets = (envelope.get("data") or {}).get("assets") or []
if not assets:
    problems.append("no asset")
for asset in assets:
    verification = asset.get("verification") or {}
    if verification.get("status") != "passed":
        problems.append("%s: verification %s" % (asset.get("path"), verification.get("status")))
if problems:
    print("\n".join(problems))
    sys.exit(1)
' "$1"
}

preview_assets() {
  local pkg=$1 envelope=$2 out=$3 asset preview_file rc name
  local -a assets refused=() stems=()
  mapfile -t assets < <(assets_of "$envelope")
  for asset in "${assets[@]}"; do
    preview_file="$ROOT/envelopes/$pkg-$(basename "$asset").preview.json"
    rc=0
    "$FACET" preview --json "$out/$asset" >"$preview_file" 2>"$preview_file.err" || rc=$?
    if [ "$rc" -ne 0 ]; then
      refused+=("$asset")
      continue
    fi
    assert_preview "$preview_file" || die "the $asset preview lacks its kind's fields"
    name=$(basename "$asset")
    stems+=("${name%%.*}")
  done
  [ ${#stems[@]} -gt 0 ] || die "$pkg: the preview verb previewed none of its assets"
  # A capability previews the asset its viewer shows (the transcript, the STEP,
  # the DXF) and refuses the sidecars derived from it: a refusal is fine only
  # for an asset that shares a stem with a previewed one.
  for asset in "${refused[@]}"; do
    name=$(basename "$asset")
    if is_previewed_stem "${name%%.*}"; then
      printf '  %s: sidecar, no preview\n' "$asset"
      continue
    fi
    show_failure "$ROOT/envelopes/$pkg-$name.preview.json" "preview $asset"
    die "$pkg: $asset has neither a preview nor a previewed sibling"
  done
}

is_previewed_stem() {
  local stem=$1 known
  for known in "${stems[@]}"; do
    if [ "$known" = "$stem" ]; then
      return 0
    fi
  done
  return 1
}

assert_preview() {
  python3 -c '
import json
import os
import sys

envelope = json.load(open(sys.argv[1], encoding="utf-8"))
if envelope.get("ok") is not True:
    print("the preview envelope is not ok")
    sys.exit(1)
preview = (envelope.get("data") or {}).get("preview")
if not isinstance(preview, dict):
    print("no preview data")
    sys.exit(1)
kind = preview.get("kind")
schema_dir = os.path.join(sys.argv[2], "schemas", "preview")
schema_path = os.path.join(schema_dir, "%s.schema.json" % kind)
if not os.path.isfile(schema_path):
    if kind != "text":
        print("no preview schema for kind %r" % (kind,))
        sys.exit(1)
    schema_path = os.path.join(schema_dir, "generic.schema.json")
schema = json.load(open(schema_path, encoding="utf-8"))
missing = [field for field in schema["required"] if field not in preview]
if missing:
    print("the %s preview misses: %s" % (kind, ", ".join(missing)))
    sys.exit(1)
' "$1" "$SNAP"
}

check_discovery() {
  step_begin "discovery (list, skill)"
  "$FACET" list --available --json >"$ROOT/envelopes/list.json"
  assert_list "$ROOT/envelopes/list.json" ||
    die "facet list --available does not show the v0.1 packages under their facets"
  "$FACET" skill kokoro --json >"$ROOT/envelopes/skill.json"
  assert_skill "$ROOT/envelopes/skill.json" ||
    die "facet skill kokoro has no body or no machine state"
  step_end
}

assert_list() {
  python3 -c '
import json
import sys

expected = {"kokoro": "voice", "whisper": "voice", "ffmpeg": "audio", "cad": "cad", "ascii": "text"}
rows = json.load(open(sys.argv[1], encoding="utf-8"))["data"]["packages"]
found = {row.get("name"): row.get("facet") for row in rows}
problems = [
    "%s: facet %r, expected %r" % (name, found.get(name), facet)
    for name, facet in expected.items()
    if found.get(name) != facet
]
if problems:
    print("\n".join(problems))
    sys.exit(1)
' "$1"
}

assert_skill() {
  python3 -c '
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))["data"]
body = data.get("body")
state = data.get("state")
problems = []
if not isinstance(body, str) or not body.strip():
    problems.append("body is empty")
if not isinstance(state, dict) or not state:
    problems.append("no machine state")
if problems:
    print("\n".join(problems))
    sys.exit(1)
' "$1"
}

dir_bytes() {
  local bytes
  bytes=$(du -sb "$1" 2>/dev/null | awk 'NR == 1 { print $1 }') || bytes=0
  printf '%s' "${bytes:-0}"
}

download_bytes() {
  local total=0 dir
  for dir in "$FACET_HOME" "$UV_CACHE_DIR" "$UV_PYTHON_INSTALL_DIR"; do
    total=$((total + $(dir_bytes "$dir")))
  done
  printf '%s' "$total"
}

print_summary() {
  local line total
  printf '\n== acceptance summary ==\n'
  if [ ${#SUMMARY[@]} -gt 0 ]; then
    for line in "${SUMMARY[@]}"; do
      printf '  %s\n' "$line"
    done
  fi
  if [ "$TOTAL_T0" -gt 0 ]; then
    printf '  %s\n' "$(summary_line "total" \
      "$(seconds "$((($(date +%s%N) - TOTAL_T0) / 1000000))")")"
  fi
  total=$(download_bytes)
  printf '  %s\n' "$(summary_line "download size" "$(gib "$total") ($total bytes)")"
  printf '  %s\n' "$(summary_line "root" "$ROOT")"
}

cleanup() {
  if [ -z "$ROOT" ] || [ "$ROOT_CREATED" -eq 0 ]; then
    return 0
  fi
  if [ "$KEEP" -eq 1 ]; then
    printf 'kept %s\n' "$ROOT"
  else
    rm -rf "$ROOT"
    printf 'removed %s (pass --keep to keep it)\n' "$ROOT"
  fi
  ROOT=""
  ROOT_CREATED=0
}

main() {
  parse_args "$@"
  trap cleanup EXIT
  prepare_root
  build_wheel
  install_and_run
  run_examples
  check_discovery
  print_summary
}

main "$@"
