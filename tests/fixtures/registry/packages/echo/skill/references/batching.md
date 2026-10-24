# Batching echo pieces

Send one piece per run. The echo runner holds no state between runs,
so batching buys nothing and only hides which input broke.
