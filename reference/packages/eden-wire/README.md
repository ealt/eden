# eden-wire

HTTP wire binding for the EDEN protocol — a FastAPI server that exposes a `Store` over the contract specified in [`spec/v0/07-wire-protocol.md`](../../../spec/v0/07-wire-protocol.md), and an `httpx`-backed `StoreClient` that satisfies the same `Store` Protocol from the opposite side.

The binding is transport-only. Behavior semantics live in chapters 4, 5, 6, and 8; this package translates them to HTTP shapes.
