---
name: poster
description: Post text with a declared credential; the secret fixtures' package.
---
# Poster

Poster posts text using credentials the manifest declares, so tests can
exercise `facet secret` and `facet auth`. Its capability is poster.text.

## When to use

Use poster when testing stored credentials, the required-secret gate and
output masking — never for a real post.

## Pieces

```toml
[inputs]
text = "hola"
```

## Judging the result

`verify` compares `post.txt` with the piece's text.

## Pitfalls

- The required secret must be stored before the first run.
- The runner prints the password on purpose so the mask is observable.
