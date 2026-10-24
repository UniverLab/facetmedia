# `facet rm`

`facet rm <package>` removes every environment of one package — every version
directory under `~/.facet/envs/<package>/` — and reports what it freed. The
package's models stay, because downloading them again is the expensive part;
`--models` deletes them too, but only the ones no other installed package
lists.

```bash
facet rm kokoro                       # environments only
facet rm kokoro --models              # environments and their own models
facet rm kokoro --json                # {"removed": [...], "freed_bytes": N}
facet rm kokoro --no-wait             # fail at once instead of waiting for the lock
```

`facet rm` holds the [facet lock](home.md#the-lock) exclusively, so it never
deletes an environment under a running job. Removing a package that is not
installed is a no-op with exit 0, so a script may remove twice.

A model directory survives when *another installed* package lists it:
`models/direct/<id>` is shared storage, and removing one tool must not break
the next one. `--models` needs the registry snapshot for the package's model
list, so it reads the snapshot in use (`FACET_REGISTRY` when set).

## `--json`

```json
{
  "removed": ["/home/me/.facet/envs/kokoro/0.1.0"],
  "freed_bytes": 2147483648
}
```

`freed_bytes` is the size of the files deleted, measured before deleting them.
Without `--json` each removed path prints one line, then `freed N bytes`, or
`nothing to remove`.
