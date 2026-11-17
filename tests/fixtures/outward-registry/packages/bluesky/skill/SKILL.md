---
name: bluesky
description: Send text with images to the lab's brand account on Bluesky, no network in tests.
---
# Bluesky

The bluesky package sends a post to the lab's brand account: text and the
images it carries. Its only capability is bluesky.send, kind `post`,
outward.

## When to use

Use bluesky when you want to publish a piece the lab already made. This
fixture never touches a real account.

## Pieces

```toml
[inputs]
text = "We shipped it."
media = [{ image = "card.png" }]
```

The `bluesky.send` capability needs `text` and at least one media entry.

## Judging the result

`verify` re-reads the post from the network and passes only when the state
it holds matches the receipt and the media count does. Read the preview's
`generic` viewer fields before fixing the piece.

## Pitfalls

- The app-password secret must be set before the run starts.
- A post is never sent twice by accident: remove the receipt to send again.

## Authentication

Create an app password in Bluesky's settings (Settings → App Passwords), then
run `facet secret set bluesky app-password` in your own terminal and paste it
at the hidden prompt. `facet doctor bluesky.send` reports whether it is set.
