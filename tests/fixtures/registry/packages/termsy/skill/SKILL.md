---
name: termsy
description: Make one termsy asset for licence and model download tests.
---
# Termsy

The termsy package makes one test asset; it exists so licence acceptance
and model downloads have something to gate on. Its capability is
termsy.make.

## When to use

Use termsy when testing `facet license accept` and `facet pull` — never
for real assets.

## Pieces

```toml
[inputs]
```

The `termsy.make` capability takes no inputs.

## Judging the result

`verify` checks the asset exists and is non-empty. Read the preview's
`generic` viewer fields to fix a failure.

## Pitfalls

- The model licence must be accepted before the first run.
- The model must be downloaded: `facet pull termsy` first.
