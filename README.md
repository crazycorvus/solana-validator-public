# Inside the Block

How do Solana mainnet leaders order the transactions inside a block? This tool answers that
empirically: it sends its own probe transactions into the leader slots of several validators,
reads the produced blocks back, and tests a set of pre-registered ordering hypotheses.

* Pre-registered design, hypotheses and decision thresholds: [`docs/DESIGN.md`](docs/DESIGN.md)
* Full spec and task list: `docs/Inside the Block — BA Spec & Task List.md`
* Findings (dated, with "what went wrong"): `report/REPORT.md` *(generated after the live runs)*

> **Status.** The pipeline is implemented and tested offline; **no live mainnet measurement has been
> run yet**, so this README makes no claims about any validator. See [Status](#status).

## Quick start (dry-run, spends nothing)

```sh
cp .env.example .env            # put your RPC URL in it (Alchemy/Helius/Triton...), optional validators.app token
docker compose up --build       # discover -> plan -> send (dry-run) -> collect -> analyze -> report
```

Without live data, `collect`/`analyze` report that there is nothing to process and the `report` stage is
still a stub (see [Status](#status)).

Dry-run builds, signs and times every probe against real mainnet leader windows but never sends:
the dry-run transport contains no network code at all, and the sender refuses a live transport
unless `live=True`. It also **replays real windows** for 60 s (`--watch N`, 0 to skip) and reports whether we
would have made it into them: clock error, and the would-hit rate for a grid of send leads
(`window_dryrun.json`). Example from 2026-10-29: median clock error 334 ms against 847 ms windows, 61 % would-hit
at the configured lead, 70 % at the best lead. The client family and version of every target are re-read at the
start of each run (`env.json`) and any change since selection is flagged. Local development:

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/pytest
.venv/bin/itb --help
```

| Command | What it does |
|---|---|
| `itb discover` | `getClusterNodes` + `getVoteAccounts` (+ validators.app) -> `data/raw/discovery/validators.json` |
| `itb select` | picks 6 targets over distinct client families with enough 4-slot windows in 24 h -> `config/targets.yaml` |
| `itb track --seconds N` | measures how well we predict window start times (`slotSubscribe`) |
| `itb plan` | experiment matrix + worst-case cost estimate |
| `itb approve` | the human gate (T16): shows targets, cap and wallet need, you type APPROVE |
| `itb send` | dry-run: client snapshot, window replay (would-we-hit report), plan of all batches on real upcoming windows, dispatch rehearsal. With `--live --cap-sol`: the orchestrated mainnet run |
| `itb collect` | fetch finalized blocks for every target slot, build the committed copy, write `landings.csv` |
| `itb analyze` | test H1-H6 per validator -> `data/analysis/verdicts.json`; delivery, path, run-to-run and competing-traffic noise -> `data/analysis/noise.json`; charts -> `report/figures/*.png` |
| `itb report` | `report/REPORT.md` from the raw and analysis files: summary, dated measurement log, per-validator findings with evidence tables, "what went wrong" (auto-detected problems plus `report/what_went_wrong_manual.md`) |
| `itb verify` / `docker compose run verify` | re-check every claim against the chain (below) |

## Going live (explicit, capped)

```sh
export SOLANA_KEYPAIR=/path/to/key.json        # file path or JSON array; never committed, never logged
itb approve --cap-sol 0.01 --by "Your Name"    # the human gate, interactive
itb send --live --cap-sol 0.01
```

The same with Docker Compose (services in the `live` profile, never started by a plain `up`; the key is a
read-only volume):

```sh
docker compose run --rm approve --cap-sol 0.01 --by "Your Name"
ITB_CAP_SOL=0.01 SOLANA_KEYPAIR_FILE=/path/to/key.json docker compose --profile live run --rm send-live
# small test: ... send-live --only <validator id prefix> --max-batches 3
```

Live mode needs **both** flags and a key. Safety layers:

* The cap is enforced by a persisted spend ledger (`data/ledger/spend_ledger.json`). The worst-case fee of
  a whole batch is reserved **before** the window opens; if it would pass the cap, the entire batch is
  refused. The cap can only ratchet down across restarts, and there is a hard ceiling of 0.06 SOL.
* Live sending is refused unless a person has run `itb approve --cap-sol X --by NAME` and typed APPROVE. That
  writes `config/live_approval.json`, bound to the exact target list and cap; changing either invalidates it.
  The wallet must hold the cap plus the returnable payer deposits (about 0.0575 SOL for a 0.05 SOL cap).
* The run is resumable and at-most-once: a batch is recorded as started before it is sent, so
  `itb send --live --cap-sol X --resume <stamp>` can never send it twice. A batch that would fire late is
  skipped, not sent late. Payer deposits are swept back at the end, also after a crash.
* Each probe has its own fee payer (see [Interaction of our own transactions](#4-interaction-of-our-own-transactions)).
  Payers are derived from your key and need a rent-exempt balance (about 0.00094 SOL each). That money is
  **not** spend and is returned by `fund.sweep`; only fees count against the cap.
* Keys: `.gitignore`, `scripts/secret_scan.sh` (pre-commit) and no key material in any log line.

## Verifying our claims

```sh
docker compose run verify
```

For every probe that landed, `verify` re-fetches the finalized block from chain and asserts the slot, the
index in the block and the leader; for every probe we call dropped it asks the chain whether it really
did not land; it recomputes each rank from the verified positions and re-derives the verdicts from
`landings.csv`, which must match the committed file exactly. It exits non-zero on any mismatch. You need
your own RPC URL, and an RPC with history that still covers the measured slots.

## How the design handles each consideration

The experiment varies one factor at a time inside a batch sent to a single leader and reads the
positions from that leader's block. Batch types (one leader window each): fee ladder, reward-per-CU,
reverse order (two batches), conflict set, timing spread, control.

### 1. Leader schedule and leader types
The window is predicted from `getLeaderSchedule`; only complete, aligned 4-slot windows of one leader are
used. The client family is snapshotted per run from `getClusterNodes` and validators.app's
`software_client` (a version string alone cannot tell Agave from Jito or BAM). Targets are chosen over
distinct families, with plain Agave always included as the documented baseline. The families on mainnet
are fine-grained (Agave, Jito-Agave, Agave+BAM, Firedancer, Firedancer+BAM, Harmonic builds, ...), so the
report groups by what was actually observed at measurement time.

### 2. Path from our backend to the executing node
The RPC path is implemented (`sendTransaction`, no preflight, no provider retry, pre-warmed connections,
microsecond send timestamps). Direct TPU/QUIC to the leader, which the spec names as the primary path, is
**not implemented yet** (see [Status](#status)). Probes that land in another leader's slot, or outside
the 4-slot window, are classified (`landed_other_leader`, `landed_outside_window`) and excluded from the
ordering tests, but counted and reported.

### 3. (Window timing) how well we hit the window
Mainnet slots measured about **0.214 s** on 2026-10-28 (606 s for 2 828 slots), so a 4-slot window lasts
only ~0.86 s. The window start is predicted from `slotSubscribe` notifications, and that clock is the weak
point: over 330 s and 323 windows on Alchemy the median prediction error was 400-470 ms (90th percentile
~1 s, 11 % within 100 ms). The provider's notification jitter is of the same order as the window itself.
We therefore do **not** treat the prediction as proof of delivery; the check is where each probe actually
landed (`landings.csv`), and probes outside their window are reported, not hidden.

First capped live test (2026-10-28, plain Agave validator, RPC path, 14 probes): 0 of 14 landed in the
target leader's window. 8 landed 5 slots after it (sent ~0.8 s late because of a bad first clock fit) on the
next leader; 6, sent 71 ms after the real window start, **never landed**. See `report/REPORT.md`, "What went
wrong". Whether the RPC path can deliver into a specific leader's window is therefore still open.

### 4. Interaction of our own transactions
Every probe uses its **own fee payer** and writes only its own fresh account, so nothing serialises our
batch by accident (a shared payer would be write-locked by every transaction and look like a conflict).
Only the conflict batch deliberately shares one writable account between half of its probes. Each tx
carries a memo `probe:<run>:<batch>:<idx>` and all parameters are logged.

### 5. Competition inside the block
Positions are measured **relative to our own probes**: the rank among our landed probes of the same batch,
so unrelated traffic does not distort the statistic. Only probes that landed in the **same slot** are compared
with each other: across slots the order is just arrival time and would look like FIFO whatever the rule is. Other transactions' fee and compute-unit data is kept
in the compact block files as context (`cu_price`, `cu_limit`, `cu_used`, `fee`).

### 6. Send order
Send order is seeded-random and recorded per tx. The reverse-order pair (fees ascending vs descending in
send order) separates a fee rule from arrival order; the reward-per-CU batch is built so that a price
sorter and a reward-per-CU sorter give *opposite* orders (an earlier draft could not tell them apart; fixed
before any live run and dated in `docs/DESIGN.md`).

### 7. Noise versus rule
Control batches (identical txs, shuffled send), at least 3 independent runs per validator on different
windows, permutation tests (seeded, 10 000 permutations), pairwise consistency, Holm correction, and a
decision rule written down in advance. "No stable rule" is a valid verdict (`H6/noise`), as are `mixed` and
`insufficient data`. On synthetic chains with a planted rule the engine recovers fee, reward-per-CU and
arrival order (also with 10 % adjacent-swap noise) and reported no false rule on 40 random-order sets.
That validates the code, not any real validator.

### Alpenglow
All measurements must be taken after the 21 Oct 2026 activation; every raw file carries its date and
`env.json` records the provider and clock drift. Pre-21-Oct data is never mixed in.

## What this design does NOT separate

* **Network jitter vs scheduler order.** Two txs sent microseconds apart can arrive at the leader in either
  order; we record send time, not arrival time at the leader.
* **Our provider's path.** On the RPC path our txs go through the provider's own forwarding, which is not
  stake-weighted for us and may batch or reorder. The effect of stake-weighted QoS on our connection is not
  separated either.
* **Jito block-engine vs native banking stage** when both are active on a validator. We send ordinary
  transactions only (no bundles, no tips).
* **Entry / micro-block structure.** `getBlock` returns no entry boundaries, so batching within entries
  cannot be tested; hypothesis H5 is reduced to "do equal-fee transactions land in arrival order".
* **Small samples.** About 6-8 probes per batch and 3 runs per validator give little power for weak or
  partial rules; the account-conflict test (H4) in particular only detects a strong effect.
* **Cost model of the scheduler.** H2 uses (priority fee + base fee / 2) / requested CU limit as a proxy for
  Agave's reward-per-cost; a different cost model is not distinguished from it.

## Data layout

```
data/raw/<run_id>/
  probes.csv        every probe: id, signature, cu_price, cu_limit, accounts_key, send_order, send_ts_us, path, fee
  send_log.jsonl    structured send/refusal log
  leaders.json      slot -> leader identity
  blocks/<slot>.json.gz   FULL block for every slot that contains one of our probes
  blocks/<slot>.json      compact block (sig, err, fee, cu_used, cu_price, cu_limit) for all other slots
  landings.csv      probe -> slot, leader, tx index, status, outcome, rank in batch
  verify_report.json
data/raw/discovery/validators.json
data/analysis/verdicts.json
data/ledger/spend_ledger.json
```

Full blocks are about 2 MB each; keeping every neighbour block in full would be gigabytes, so neighbours
are compacted. Anything not committed can be re-fetched by signature with `verify`.

## Reproducibility

Pinned Python image and dependency versions, seeded randomisation of probe parameters and of every
permutation test, and analysis that is a pure function of the raw files. `docker compose up` runs the
whole pipeline in dry-run on a clean machine.

## Status

Implemented and tested (64 offline tests pass plus 2 against a real `solana-test-validator`, including the full orchestrator against a real `solana-test-validator`): discovery and target selection on real mainnet data, leader
schedule and window tracking, probe builder, seeded experiment matrix, persisted spend ledger, sender
(dry-run and RPC), block collector and landing mapper (checked against real finalized blocks, including
version-1 transactions), the inference engine (validated on synthetic chains), the verify service.

Not done yet:

* **No live measurement**: nothing has been sent on mainnet, so there are no results.
* **Direct TPU/QUIC sender**: only the RPC path exists. If the RPC landing rate is too low this is the
  likely next piece (probably in Rust).
* **Devnet end-to-end** (localnet end-to-end passes: fund payers, live RPC send, collect, map, `verify`).
  Localnet block order is not evidence about mainnet leaders, it only proves the plumbing.
