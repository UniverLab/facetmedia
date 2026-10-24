---
name: future
description: A future capability no current facet release can install.
---
# Future

The future package declares a facet requirement no current release
satisfies; it is how tests see an incompatible package. Its capability
is future.thing.

## When to use

Use future when testing incompatible packages — never for real assets.

## Pieces

```toml
[inputs]
```

The `future.thing` capability takes no inputs.

## Judging the result

`verify` never runs: the package cannot be installed. Read the preview's
`generic` viewer fields to fix a failure.

## Pitfalls

- Incompatible means un-installable: `facet pull future` always fails.
- It still lists, so clients can see what a newer facet would offer.
