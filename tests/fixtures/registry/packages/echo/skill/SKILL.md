---
name: echo
description: Echo text back for smoke tests of pieces, runs and previews.
---
# Echo

The echo package repeats text back; it is the smoke test for the whole
`facet run` path. Its only capability is echo.text.

## When to use

Use echo when you want to check that pieces, runs, records and previews
work at all — never for real assets. If the text matters, use a real
package instead.

## Pieces

One complete piece per capability, in a fenced `toml` block:

```toml
[inputs]
text = "hello"
repeat = 2
```

The `echo.text` capability needs `text` and repeats it `repeat` times.

## Judging the result

`verify` checks that the asset holds the repeated text; `echo.text`
thresholds are empty, so any run output passes. When a run fails, read
the preview's `generic` viewer fields before fixing the piece.

## Pitfalls

- `repeat` must stay within its `min` and `max`, or the schema rejects it.
- `text` is required: an empty piece fails before the runner starts.
