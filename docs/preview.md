# `facet preview`

`facet preview` shows one asset facet made: to an agent as data, to a
person through the capability's viewer. Both forms speak the same `--json`
contract as everything else and take no lock.

```bash
facet preview out/echo.txt --json              # the record plus the preview file
facet preview out/echo.txt                      # serve the viewer, open the browser
facet preview out/echo.txt --no-open            # serve the viewer, print the URL only
facet preview out/echo.txt --port 8901 --idle-timeout 60
```

The argument names the asset or its record. The record's provenance names
the capability, whose package must still be installed: when its
environment is gone facet fails with `not_installed` and the hint
`facet pull <package>`.

When `<asset>.preview.json` is missing or older than the asset, facet calls
the runner's `preview` verb to write it; otherwise the file on disk is
used. The images a preview lists are only ever files beside the asset.

## Agent form: `facet preview <asset> --json`

Nothing is opened and no server starts. The data is
`{"asset", "record", "preview", "images"}`: the asset's path relative to
the working directory, its record, the parsed preview file, and the
preview's images as absolute paths.

## Person form: `facet preview <asset>`

The viewer is the capability's `preview.viewer` (default `generic`) from
the registry snapshot in use, at `viewers/<viewer>/`; the shared kit lives
at `viewers/_kit/`. Facet serves them on 127.0.0.1 — a free port, or
`--port` — read-only and only three paths: `/viewer/...` from the viewer's
directory, `/kit/...` from the kit, and `/asset/<name>` for the asset, its
record, its preview file and its images. Anything else is 404, and so is
any path with `..`.

The viewer opens at `http://127.0.0.1:<port>/viewer/index.html?asset=<name>`
(`wslview`, `explorer.exe`, `xdg-open` or `open`, whichever fits; with
`--no-open`, or when no opener works, the URL is printed instead). The
server runs in the foreground, prints the URL to stdout, and stops on
Ctrl+C or after `--idle-timeout` seconds without requests (default 600). A
viewer missing from the registry falls back to `generic`; when that is
missing too, facet prints the preview JSON and exits 0. The core carries no
HTML of its own.
