# Inside the Block

How do Solana mainnet leaders order the transactions inside a block? This tool answers that
empirically: it sends its own probe transactions into the leader slots of several validators,
reads the produced blocks back, and tests a set of pre-registered ordering hypotheses.

* Pre-registered design, hypotheses and decision thresholds: [`docs/DESIGN.md`](docs/DESIGN.md)
* Full spec and task list: `docs/Inside the Block — BA Spec & Task List.md`
* Findings (dated, with "what went wrong"): `report/REPORT.md` *(generated after the live runs)*

> **Status.** The study has been run on mainnet (2026-10-29, after Alpenglow) and verified against the chain:
> 8 validators in 7 client families, 4-5 runs each, 107-147 probes per validator inside the target leader's
> window. Read the [Results](#results-2026-10-29) and, above all, what they do **not** show.

## Quick start (dry-run, spends nothing)

```sh
cp .env.example .env            # put your RPC URL in it (Alchemy/Helius/Triton...), optional validators.app token
docker compose up --build       # discover -> plan -> send (dry-run) -> collect -> analyze -> report
```

Without live data, `collect`/`analyze` report that there is nothing to process and `report` writes an empty
report; with the raw data of a live run in `data/raw/`, the same command regenerates the full report.

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
| `itb select [--n N]` | picks N targets (default 6) over distinct client families with enough 4-slot windows in 24 h -> `config/targets.yaml` |
| `itb track --seconds N` | measures how well we predict window start times (`slotSubscribe`) |
| `itb plan` | experiment matrix + worst-case cost estimate |
| `itb approve` | the human gate (T16): shows targets, cap and wallet need, you type APPROVE |
| `itb send` | dry-run: client snapshot, window replay (would-we-hit report), plan of all batches on real upcoming windows, dispatch rehearsal. With `--live --cap-sol`: the orchestrated mainnet run |
| `itb collect` | fetch finalized blocks for every target slot, build the committed copy, write `landings.csv` |
| `itb analyze` | test H1-H6 per validator -> `data/analysis/verdicts.json`; delivery, path, run-to-run and competing-traffic noise -> `data/analysis/noise.json`; charts -> `report/figures/*.png` |
| `itb report` | `report/REPORT.md` from the raw and analysis files: summary, dated measurement log, per-validator findings with evidence tables, "what went wrong" (auto-detected problems plus `report/what_went_wrong_manual.md`) |
| `itb verify` / `docker compose run verify` | re-check every claim against the chain (below) |

## Going live (explicit, capped)

### 1. Create a dedicated key file (live mode only)

Dry-run, `collect`, `analyze`, `verify` and `report` need **no key**. Live sending needs one funded keypair,
because the transactions have to pay fees. Use a **new wallet created only for this study** and never your main one.
A keypair file is a JSON array of 64 numbers (the standard Solana format).

```sh
# Option A: Solana CLI (Agave installer: https://release.anza.xyz)
mkdir -p ~/.config/itb && chmod 700 ~/.config/itb
solana-keygen new --no-bip39-passphrase -o ~/.config/itb/key.json
chmod 600 ~/.config/itb/key.json
solana-keygen pubkey ~/.config/itb/key.json          # the public address to fund

# Option B: no Solana CLI, only this project's environment
mkdir -p ~/.config/itb && chmod 700 ~/.config/itb
.venv/bin/python - <<'PY'
import json, os
from pathlib import Path
from solders.keypair import Keypair
k, p = Keypair(), Path.home() / ".config/itb/key.json"
fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)   # refuses to overwrite an existing key
with os.fdopen(fd, "w") as f:
    json.dump(list(bytes(k)), f)
print("address to fund:", k.pubkey())
PY
```

Rules for the file:

* Keep it **outside the repository** (`.gitignore` and `scripts/secret_scan.sh` block common names, but the safest
  place is somewhere git never looks). Never paste its contents into a chat, an issue or a log.
* It has no passphrase, so anyone who can read your account can spend from it: fund it with what the study needs
  and nothing more.
* **Fund the address yourself from your normal wallet.** The wallet must hold the fee cap plus the rent floor of the
  8 payer accounts (650 240 lamports each, returned at the end) plus a small margin, i.e. about
  `cap + 5.25 million lamports`: **0.0153 SOL for a 0.01 SOL cap**. `itb approve` prints the exact figure. Check it
  with `solana balance <address> --url mainnet-beta` or any explorer.
* Unspent funds are swept back to this wallet at the end of a run. When the study is over, move the remainder to
  your own wallet and delete the file (`rm ~/.config/itb/key.json`).

Then point the tool at it, either as a path or inline (never commit either):

```sh
export SOLANA_KEYPAIR=$HOME/.config/itb/key.json     # or put the line in .env (gitignored)
# Docker Compose: pass the path instead, it is mounted read-only
export SOLANA_KEYPAIR_FILE=$HOME/.config/itb/key.json
```

### 2. Sign off and run

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

## Results (2026-10-29)

Probes that landed in the target leader's 4-slot window, compared only with other probes of the same batch that
landed in the **same slot**. Effect = Kendall tau (1 = the rule explains the order); a rule is declared only if the
pooled effect is >= 0.6, p < 0.01, at least 2/3 of runs agree and no batch type contradicts it (see
`docs/DESIGN.md`). Generated from `data/analysis/verdicts.json`; full tables, evidence and signatures in
`report/REPORT.md`.

| Validator | Client (at measurement) | In-window probes | H1 fee/CU | H2 reward/CU | H3 arrival (mixed fees) | H4 shared account | H5 equal-fee arrival | Verdict |
|---|---|---|---|---|---|---|---|---|
| `5Us18hLZ` | agave 4.3.2 | 107 | -0.10 | +0.06 | +0.01 | -0.25 | **+0.86** | equal-fee arrival order only |
| `7QQGNm3p` | agave 4.3.2 | 147 | -0.13 | +0.12 | -0.02 | +0.25 | **+0.86** | equal-fee arrival order only |
| `9eGrDohd` | firedancer 26.10.4 | 129 | +0.66 | **+0.84** | -0.06 | +0.25 | **+0.75** | reward-per-CU (H2) + equal-fee arrival |
| `9jxgosAf` | firedancer+BAM 26.10.5 | 135 | +0.23 | +0.58 | +0.07 | +0.50 | **+0.69** | equal-fee arrival order (H2 partial, below threshold) |
| `DRpbCBMx` | agave+BAM 4.4.0-rc.1 | 131 | +0.01 | +0.44 | -0.10 | +0.38 | **+0.81** | equal-fee arrival order (H2 partial) |
| `Fd7btgyS` | harmonic-agave 4.3.2 | 142 | +0.05 | +0.09 | +0.07 | **+0.59** | **+0.74** | shared-account probes land later + equal-fee arrival (confidence 0.75) |
| `HEL1USMZ` | jito-agave 4.3.2 | 144 | +0.16 | +0.14 | +0.05 | -0.17 | **+0.73** | equal-fee arrival order (confidence 0.67) |
| `JUPiTERr` | harmonic-firedancer 26.10.4 | 130 | +0.16 | +0.20 | -0.25 | +0.09 | **+0.92** | equal-fee arrival order only |

What the data supports:

* **Among probes with identical fees, later sends land later (same slot) on 8 of 8 validators**, so a first-come
  first-served tie-break is the one rule seen everywhere (H5; the pre-registered version was trivially true and
  was changed after the fact, see `docs/DESIGN.md`, Amendments).
* **A fee-based rule is not visible on most validators.** H1 (price per CU) is near zero for 6 of 8, and the
  *other* transactions in the same blocks are not sorted by fee either (competing-traffic tau about 0.0 for all
  8, `noise.json`). The exception is Firedancer `9eGrDohd`, where ordering follows (priority fee + base fee / 2)
  per requested CU (H2 +0.84; H1 +0.66 is the same direction because price and reward agree except in the batch
  built to separate them). `9jxgosAf` (+0.58) and `DRpbCBMx` (+0.44) lean the same way but stay below our own
  threshold, so they are reported as no firm rule.
* **One validator shows a conflict effect** (`Fd7btgyS`: probes sharing a writable account land after disjoint
  ones, +0.59, p 0.006); it rests on one validator and is the weakest claim here.

What it does **not** show (read before quoting any number):

* **Our probes arrive as a burst.** Batches b1-b4 are dispatched within ~8 ms through an RPC provider, so the
  leader's scheduler may never hold more than one of them at a time; a fee-priority scheduler can only reorder
  transactions that are queued together. "No fee rule" therefore means "no fee rule detectable at this arrival
  pattern", not "the leader ignores fees". Only the Firedancer validator reordered inside such a burst.
* **The provider path is not separated.** Every probe goes through Alchemy's forwarding; its ordering and
  batching are part of what we measured. In control batches (identical transactions, shuffled sends) `5Us18hLZ`
  showed tau -0.71 against send order (later sends landing *earlier*), the others -0.24..+0.21, which we cannot
  explain from the data and may be forwarding behaviour.
* **Only 49-65 % of probes landed inside their window** (the rest landed on the next leader because our slot
  clock is accurate to ~0.4 s against 0.85 s windows); the in-window subset is selected by timing, not random.
* **About 130 probes per validator, 4-5 runs**, is little power for weak rules; Holm-corrected p-values are in the
  report.

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
wrong".

Second capped live test (2026-10-29, Jito-Agave validator `HEL1USMZ`, RPC path, 22 probes): the batch sent 284 ms
*before* the observed window start landed 6 of 6 in slot +0, inside the window; the two batches sent 641 ms and
1 147 ms *after* it landed 4-7 slots late on the next leader. So in-window delivery through the provider works
when the send is early enough (the round trip is ~200-280 ms), and the misses were clock error, not refusal. The
plain Agave validator in the first test is the open case: 0 of 6 in-window probes landed at all.

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
  blocks/<slot>.json.gz   every collected slot, COMPACT and gzipped: all transactions of the block in order
                          (signature, error, fee, CU used, requested price/limit, vote flag), enough to verify
                          positions; the full block is one `getBlock` call away (`verify` does exactly that)
  landings.csv      probe -> slot, leader, tx index, status, outcome, rank in batch
  verify_report.json
data/raw/discovery/validators.json
data/analysis/verdicts.json
data/ledger/spend_ledger.json
```

Full blocks are about 2 MB each (hundreds of MB for a study), so only the compact form is committed (65 MB
for the whole study). `collect` keeps the full blocks in the git-ignored `data/cache/` and can keep full gzipped
blocks for probe slots on request (`keep_full`).

## Reproducibility

Pinned Python image and dependency versions, seeded randomisation of probe parameters and of every
permutation test, and analysis that is a pure function of the raw files. `docker compose up` runs the
whole pipeline in dry-run on a clean machine.

## Status

Done and verified: discovery and target selection on real mainnet data, slot clock and window replay, probe
builder, seeded experiment matrix, persisted spend ledger (with an interactive on-chain reconciliation), sender
(dry-run and RPC), resumable at-most-once orchestrator behind a human sign-off, block collector and landing
mapper, inference engine, noise analysis, charts, report generator and the `verify` service. 71 offline tests plus
2 against a real `solana-test-validator`. The mainnet study of 2026-10-29 ran 8 validators in 7 client families
(4 runs each, 107-147 in-window probes per validator) and `itb verify` passes on all four run folders: 1 776
landed probes re-checked on chain, 1 268 drops confirmed, verdicts re-derived identically. Fees paid on chain:
0.0091 SOL against a 0.05 SOL budget and a 0.01 SOL approved cap.

Known gaps:

* **Direct TPU/QUIC sender is not implemented**: every probe goes through one RPC provider (Alchemy), which is
  part of what was measured. A direct sender would be the natural next experiment, and a second provider would
  show how much of the ordering belongs to the provider.
* **Bursty arrival**: see *What it does not show* in [Results](#results-2026-10-29).
* **No devnet run**: devnet has one client and nothing to measure; localnet passes the whole pipeline end to end
  but its block order says nothing about mainnet leaders.
* **The first full run failed through our own bug** (payer deposits too small) and was re-sent; the failed
  probes are kept, marked as excluded, and described in `report/REPORT.md` under "What went wrong".
