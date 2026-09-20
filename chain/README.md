# On-chain score settlement (Solana devnet)

Every point a run scores becomes one SPL token, minted in **one transaction per
run** rather than one per kill.

## Why it is a separate process

It is a **read-only poller**. It GETs the same `/telemetry` the dashboard
already reads, notices a run ended, and settles. Nothing calls it; it calls
nothing but the Pi and an RPC endpoint.

- no change to `game/` — the phone does not know it exists
- no change to `controller-bridge/` — the Pi is only ever read from
- no change to the `/world` path
- `kill -9` on this process changes nothing about the game

That last point is the whole reason for the shape, and it is verified, not
assumed — see *Verified* below.

## Why batches, and why this chain

A transaction inside the combat loop would cost frames, so settlement happens on
the boundaries the game already has: **death**, and an **explicit trigger**.

Batching is only economically sane because a Solana signature is ~5000 lamports.
A whole evening of play settles for a fraction of a cent, which is what makes
"one token per point" a real design rather than a database with extra steps. On
a chain with dollar-scale fees you would be forced into an off-chain ledger and
a periodic bridge — at which point the chain is decoration.

## Devnet only, permanently

The process **refuses to start** against a non-devnet RPC. The token is a score,
not a financial instrument, and a mainnet URL here would let it be mistaken for
one.

## Keys

`chain/keys/*.json`, mode 600, gitignored — the same JSON-array-of-bytes shape
`solana-keygen` writes, so the CLI can read them. Two keys: the **mint
authority** (also the fee payer) and the **player wallet** that receives the
tokens. Nothing here ever runs in a browser.

## Running it

```bash
cd chain
./.venv/bin/python jzchain.py setup     # create + fund the mint and wallet (once)
./.venv/bin/python jzchain.py serve     # poll and settle; panel on :8100
./.venv/bin/python jzchain.py settle    # settle everything outstanding, now
./.venv/bin/python jzchain.py status    # print state and exit
./.venv/bin/python test_jzchain.py      # 44 assertions, no network needed
```

The panel is at **http://localhost:8100/** — unsettled score, token balance, the
last signature and a **Solscan link**, which is the thing a judge actually
checks. `SETTLE NOW` makes a transaction happen without anyone having to die.

## Settling the right number

The game's score does **not** reset on death (`player.reset()` restores hearts,
not points), so a run's takings are the delta since whatever has already been
minted. That makes a second death settle only what was earned after the first,
and makes "unsettled" a number that means something on screen. Attaching to a
session already in progress claims none of its history.

Triggers: **death**, **manual**, and **the phone going quiet** for 25 s — the
last so a session that ends without dying is not stranded.

## Failure

Never raises. Devnet RPC is flaky and rate-limited, so every call retries with
exponential backoff and jitter, then gives up *quietly*. A settlement that did
not land is marked `failed`, **kept in the queue**, retried with growing backoff
capped at two minutes, and shown as failed on the panel. `last_confirmed()`
ignores failures by construction, so a failure can never be rendered as the last
successful transaction. The queue is persisted, so a crash cannot lose a run.

## Verified

- the game is untouched by this process starting, running, and being `kill -9`ed
  mid-session: telemetry seq, session clock, fps, SSE clients and bridge uptime
  all continuous across the kill
- 44 assertions on the settlement arithmetic, the queue and the failure paths
- devnet RPC reachable and answering

## Not verified

See the note in the handover: the first real transaction needs devnet SOL, and
the faucet is rate-limited per day on this IP.
