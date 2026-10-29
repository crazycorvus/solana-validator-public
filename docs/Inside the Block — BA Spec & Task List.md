# Inside the Block — BA Spec & Task List

Oct 28, 2026 · @Art Nik

## 1. Executive summary

Build a one-command, Dockerised measurement tool that sends our own transactions into the leader slots of 6+ Solana mainnet validators (3+ client families), then infers from the produced blocks what ordering rule each leader uses. Spend cap ≈ 0.05 SOL. Ship repo + raw data + README + dated report before the deadline.

| Item | Value |
| --- | --- |
| Listing | [Inside the Block — Superteam Ukraine](https://superteam.fun/earn/listing/inside-the-block-how-solana-validators-order-transactions) |
| Type | Bounty, regional: Ukraine only (1 credit to submit) |
| Prize pool | 2,500 USDC — 800 / 600 / 500 / 300 / 300 |
| Submissions close | \~5 Nov 2026 (8d 5h left as of 28 Oct) |
| Winners announced | Nov 19, 2026 |
| Competition | 10 submissions so far; several non-UA teams publicly posted full solutions in comments |
| Skills | Backend, Blockchain (Solana RPC, tx building, data analysis) |

**Judging weights:** Design validity 35% · Verifiability & reproducibility 25% · Analysis quality 25% · Engineering 15%. Design and honesty of analysis (60%) outweigh code polish.

**Key decision:** Alpenglow went live 21 Oct 2026, so all our measurements are post-Alpenglow by default. Do not reuse any pre-21-Oct data.

## 2. Problem and research question

The leader's scheduler decides intra-block order. Agave's rule is readable in open source; the forks holding most stake (Jito-Solana, Firedancer/Frankendancer, Paladin, BAM, etc.) are not documented. The sponsor wants empirical evidence, not code reading.

**Research question (per validator):**

1. What decides whether tx A lands before tx B in the block?
2. How strictly does that rule hold (% of pairs consistent)?
3. Is there a rule at all, or is the order indistinguishable from noise?

**Candidate hypotheses to test (H0 = no rule):**

| ID | Hypothesis | Controlled variable | Expected signature in block |
| --- | --- | --- | --- |
| H1 | Priority-fee ordering (higher fee-per-CU first) | Compute-unit price | Monotonic fee vs. position |
| H2 | Priority / reward-per-CU (Agave prio-graph) | CU price × CU limit | Order by fee ÷ requested CU |
| H3 | Arrival order (FIFO) | Send timestamp / send order | Position tracks send order |
| H4 | Account-lock conflicts dominate | Shared vs. disjoint writable accounts | Conflicting txs serialized, disjoint ones parallel/unordered |
| H5 | Batched / micro-block ordering | Send timing across slot | Clusters per entry; order inside entry differs |
| H6 | No stable rule (noise) | All | Rank correlation ≈ 0, non-reproducible |

"No rule / it's noise" is an accepted answer if it is backed by data.

## 3. Scope, constraints, assumptions

**In scope**

- Solana **mainnet** measurements only (localnet/devnet only for building and testing).
- ≥ 6 validators across ≥ 3 client/fork versions, identified by `getClusterNodes` version string and validators.app at measurement time.
- Jito-Solana validators, but only their regular scheduler path.
- Primary evidence = transactions we send ourselves; other block txs are supplementary context.

**Out of scope**

- Jito bundles and tips.
- Mixing pre- and post-Alpenglow (21 Oct 2026) data.
- MEV extraction or any adversarial behaviour.

**Hard constraints**

| Constraint | Rule |
| --- | --- |
| Fee budget | Code-enforced cap, whole study ≈ 0.05 SOL |
| Default mode | Dry-run; live sending only behind explicit flag + cap |
| Keys | Volume or env only; never committed |
| Run | One command: `docker compose up` |
| Raw data | Every signature + slot published so judges can `getBlock` it |
| Report | Date of every measurement + "what went wrong" section |

**Assumptions**

- Base fee 5,000 lamports/signature → 0.05 SOL ≈ 10,000 signatures at zero priority fee; realistic budget with priority fees ≈ 400–1,500 txs.
- A paid RPC (Helius/Triton/QuickNode) with `getBlock`, `getLeaderSchedule`, `getClusterNodes` and ideally staked/direct-TPU sending is available.
- Team member is Ukraine-based (submission eligibility).

**Open questions**

- Which RPC / sending path gives the lowest-noise delivery to a specific leader (direct TPU via QUIC vs. RPC `sendTransaction`)?
- Does the sponsor count Jito-BAM and Frankendancer as distinct "versions"? Assume yes if version strings differ.

## 4. Functional requirements

| ID | Requirement | Acceptance criteria | Priority |
| --- | --- | --- | --- |
| FR-01 | Validator discovery: list mainnet validators with identity, vote key, stake, client version | Table of ≥ 30 candidates with version from `getClusterNodes` + validators.app snapshot saved to raw data | Must |
| FR-02 | Target selection: pick ≥ 6 validators covering ≥ 3 client families with upcoming leader slots | Config file with chosen validators and rationale; validated before run | Must |
| FR-03 | Leader-window detection: compute upcoming slots per target from `getLeaderSchedule` + live `slotSubscribe` | Predicts the next 4-slot window with ≥ 95% accuracy in dry-run log | Must |
| FR-04 | Probe builder: create probe tx batches with controlled variables (CU price, CU limit, account set, send order, memo with probe ID) | Each tx carries a unique memo `probe:<run>:<batch>:<idx>` and logged parameters | Must |
| FR-05 | Sender: deliver a batch to the target leader just before / during its window, via selectable path (RPC, direct TPU/QUIC) | Send timestamps logged at µs; path recorded per tx | Must |
| FR-06 | Spend cap: hard budget in lamports, checked before every send; abort on breach | Unit test proves send refused at cap; cumulative spend in log and report | Must |
| FR-07 | Dry-run mode (default): build, sign and time txs but never send; report whether we would have hit the window | No network send calls in dry-run; flag `--live --cap-sol 0.05` required to send | Must |
| FR-08 | Block collector: `getBlock` for every target slot (+ neighbours) with full tx details and entries | Raw JSON stored per slot; retries until finalized | Must |
| FR-09 | Landing analysis: map our probes to slot, leader, block index, entry index | Table of every probe: signature, slot, leader, position, landed/dropped | Must |
| FR-10 | Ordering inference: test H1–H6 per validator with rank correlation, pairwise consistency, permutation tests | Per-validator verdict + effect size + p-value + confidence | Must |
| FR-11 | Repeatability: ≥ 3 independent runs per validator on different leader windows | Run IDs and dates in report; results compared across runs | Must |
| FR-12 | Report generator: Markdown report with charts, per-validator findings, dates, "what went wrong" | `report/REPORT.md` regenerated from raw data by one command | Must |
| FR-13 | Verification helper: script that re-fetches `getBlock` for listed signatures and re-checks positions | Judge can run `docker compose run verify` and get matching positions | Should |
| FR-14 | Supplementary context: fee and CU stats of non-probe txs in same blocks | Plots of competing fee distribution per block | Could |

## 5. Non-functional requirements

| ID | Category | Requirement |
| --- | --- | --- |
| NFR-01 | Reproducibility | `docker compose up` runs the full pipeline (dry-run) on a clean machine; pinned image and dependency versions |
| NFR-02 | Security | Keypair mounted read-only via volume or `SOLANA_KEYPAIR` env; `.gitignore` + pre-commit secret scan; no keys in logs |
| NFR-03 | Safety | Live mode needs both `--live` and `--cap-sol`; spend ledger persisted so restarts cannot exceed cap |
| NFR-04 | Observability | Structured JSON logs (timestamp, run ID, probe ID, slot, leader, path, lamports spent) |
| NFR-05 | Determinism | Seeded randomisation of probe parameters; analysis is deterministic from raw data |
| NFR-06 | Time accuracy | NTP-synced clock; host clock drift recorded per run |
| NFR-07 | Performance | Send a 20–50 tx batch within < 50 ms; prepare batches ahead using recent blockhash |
| NFR-08 | Portability | Any language allowed; recommended TypeScript (`@solana/web3.js` v2 / kit) or Rust for TPU sending, Python for analysis |
| NFR-09 | Testability | Unit tests for cap, parser, inference; integration test on `solana-test-validator` in CI |
| NFR-10 | Documentation | README explains design choices, every "account for" point, and what the design does NOT separate |

## 6. Experiment design and architecture

We vary one factor at a time inside a batch sent to a single leader, then read positions from that leader's block. Each stage is its own Docker Compose service.

&#91;embedded content: measurement pipeline · 8 stages, 1 spend gate\]

Dry-run stops before the leader; everything up to Send runs on real mainnet data at zero cost.

**Probe batches (one leader window each)**

| Batch type | What varies | Held constant | Tests |
| --- | --- | --- | --- |
| Fee ladder | CU price, 8–10 levels, shuffled send order | CU limit, disjoint accounts | H1, H3 |
| Reward-per-CU | CU limit with fixed total fee | Disjoint accounts | H2 |
| Reverse order | Send order reversed vs. fee | Fees, accounts | H1 vs H3 |
| Conflict set | Shared vs. disjoint writable account | Fees | H4 |
| Timing spread | Send offset across the 4 slots | Fees, accounts | H5, path latency |
| Control | All identical | All | H6 noise baseline |

**How the design handles each listed consideration**

| Consideration | Our handling |
| --- | --- |
| Leader schedule and leader types | Window predicted from `getLeaderSchedule`; client family snapshotted per run; only consecutive-slot windows used |
| Path from backend to executing node | Direct TPU/QUIC to leader as primary; RPC path as comparison; record path, send time and landing slot; probes landing in a different leader's slot are excluded |
| Interaction of our own txs | Each probe writes only its own fresh account unless the batch tests conflicts; memo tags identify probes |
| Competition inside the block | Positions measured relative to our own probes (rank within our set), plus context stats of other txs |
| Send order | Randomised and recorded; reverse-order batches separate FIFO from fee rule |
| Noise vs. rule | Control batches, ≥ 3 repeats per validator, permutation tests; a null result is reported as such |
| Alpenglow | All data after 21 Oct 2026; dates in report |

**What the design does NOT separate (state in README):** network jitter inside one QUIC stream vs. scheduler thread order; the effect of stake-weighted QoS on our connection; Jito block-engine path vs. native banking stage when both are active.

## 7. Data model and deliverables

**Raw data files (committed under `data/raw/<run_id>/`)**

| File | Key fields |
| --- | --- |
| `validators.json` | identity, vote\_pubkey, stake, version, client\_family, source, snapshot\_ts |
| `leader_windows.json` | run\_id, validator, slots\[\], predicted\_ts, observed\_ts |
| `probes.csv` | probe\_id, run\_id, batch\_id, signature, cu\_price, cu\_limit, accounts\_key, send\_order, send\_ts\_us, path, lamports\_fee |
| `landings.csv` | signature, slot, leader, tx\_index\_in\_block, entry\_index, index\_in\_entry, status, err |
| `blocks/<slot>.json` | full `getBlock` response (json, full tx details, rewards) |
| `spend_ledger.json` | cumulative lamports, per-run totals, cap |
| `env.json` | git SHA, image digest, RPC provider, host clock drift, date |

**Repository layout**

```
inside-the-block/
  docker-compose.yml   # services: discover, plan, send, collect, analyze, report, verify
  config/targets.yaml  # validators + experiment params
  src/                 # sender + collector
  analysis/            # notebooks / scripts, deterministic
  data/raw/            # all runs
  report/REPORT.md     # dated measurements + what went wrong
  README.md            # design rationale + considerations + limits
```

**Final deliverables**

- [ ] Public GitHub repository link
- [ ] One-command Docker Compose run (dry-run default)
- [ ] Raw data with all signatures and slots
- [ ] README (why this design, how each consideration is handled, what it does NOT separate)
- [ ] REPORT.md with per-measurement dates and "what went wrong"
- [ ] Submission on Superteam Earn (1 credit)

## 8. Risks and competitive analysis

**Competition.** Public comments show at least 5 complete studies (4–22 validators, up to 6 client versions incl. Agave, Jito-Agave/BAM, Firedancer/Frankendancer, Paladin). Most are from outside Ukraine and cannot submit, but their quality sets the bar. Differentiate on design rigour and verifiability, not on validator count.

**Differentiators to aim for**

- Pre-registered hypotheses and statistical tests (written before live runs).
- Clean separation of *network path* noise from *scheduler* rule (same batch via two paths).
- A `verify` service judges can run to re-check every claim from chain.
- Explicit "what we cannot separate" list and honest null results.

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Txs miss the target leader window (forwarding, latency) | No evidence for that validator | Send to leader TPU directly; use window slots 2–4; measure landing rate in dry-run plan; drop and log misses |
| Budget exhausted early | Too few samples | Cap per run and per validator; start with zero/low priority fee; small batches |
| Our own txs conflict on accounts unintentionally | Confounds H1 vs H4 | Each probe uses fresh or controlled writable accounts; record account sets |
| Validator version changes mid-study | Mislabelled client | Snapshot version at each run; group by version actually observed |
| RPC rate limits / dropped `getBlock` | Missing data | Retry with backoff, second RPC provider |
| Ukraine-only eligibility proof | Disqualification | Confirm profile region before submitting |
| Deadline \~5 Nov | Late submission | Live runs must finish by 3 Nov; 2 days for report |

## 9. Task list for the AI factory

Each task is sized for one agent run (≈ 1–4 h). Feed tasks in ID order; a task starts only when its dependencies are done. Agent roles: **ARCH** architect, **DEV** backend developer, **DATA** analyst, **QA** tester, **DOC** technical writer, **HUMAN** you.

| ID | Epic | Task | Role | Depends on | Acceptance criteria |
| --- | --- | --- | --- | --- | --- |
| T01 | E0 Setup | Create repo skeleton, Dockerfile(s), `docker-compose.yml` with services discover/plan/send/collect/analyze/report/verify | DEV | — | `docker compose up` runs and each service prints help in dry-run |
| T02 | E0 Setup | Config loader (`targets.yaml`, env), secret handling, `.gitignore`, pre-commit secret scan | DEV | T01 | No key paths in repo; startup fails clearly if key missing in live mode |
| T03 | E0 Setup | Write pre-registered experiment design `docs/DESIGN.md`: hypotheses H1–H6, variables, tests, sample sizes | ARCH | — | Each hypothesis has variable, test, decision threshold |
| T04 | E1 Discovery | Validator discovery: `getClusterNodes` + `getVoteAccounts` + validators.app; classify client family | DEV | T02 | `validators.json` with ≥ 30 rows and client\_family set |
| T05 | E1 Discovery | Target selector: choose ≥ 6 validators, ≥ 3 families, with leader slots in next 24 h | DEV | T04 | `targets.yaml` auto-filled; selection rationale logged |
| T06 | E2 Timing | Leader schedule + slot tracker (`getLeaderSchedule`, websocket `slotSubscribe`), window predictor | DEV | T02 | Dry-run log shows predicted vs. observed window, ≥ 95% hits |
| T07 | E3 Probes | Spend ledger + hard cap module (persisted, checked pre-send) | DEV | T02 | Unit tests: refuse at cap, survive restart, never exceed |
| T08 | E3 Probes | Probe builder: memo-tagged txs with ComputeBudget CU price/limit, controlled account sets, seeded params | DEV | T03, T07 | Batch file with every parameter; sig precomputed |
| T09 | E3 Probes | Experiment matrix generator per hypothesis (fee ladder, reverse send order, shared vs. disjoint accounts, timing offsets) | ARCH | T03, T08 | Matrix fits budget; cost estimate printed |
| T10 | E4 Sending | Sender via RPC and direct TPU/QUIC to current/next leader; µs timestamps; path tag | DEV | T06, T08 | Dry-run: no send calls; live: per-tx log line |
| T11 | E4 Sending | Dry-run on mainnet: build + time all batches, report expected window hit rate | QA | T10 | Dry-run report produced, zero lamports spent |
| T12 | E5 Collection | Block collector: `getBlock` for target + neighbour slots, retries, store raw JSON | DEV | T02 | Raw block JSON stored for every target slot |
| T13 | E5 Collection | Landing mapper: probe → slot, leader, tx index, entry index | DEV | T12 | `landings.csv` complete; dropped probes flagged |
| T14 | E6 Testing | Localnet integration test on `solana-test-validator` (parser + pipeline) | QA | T13 | CI green; end-to-end on localnet |
| T15 | E6 Testing | Devnet end-to-end run with real sending | QA | T14 | One full run on devnet stored as sample data |
| T16 | E7 Live | HUMAN gate: review budget, fund wallet (≤ 0.06 SOL), approve live flag | HUMAN | T11, T15 | Signed-off cap and target list |
| T17 | E7 Live | Mainnet live runs: ≥ 3 runs per validator on different windows, dated | DEV | T16 | Raw data for ≥ 6 validators × 3 runs; spend ≤ cap |
| T18 | E8 Analysis | Inference engine: Spearman/Kendall vs. each variable, pairwise consistency, permutation test vs. random | DATA | T13 | Per-validator verdict table with p-values and effect sizes |
| T19 | E8 Analysis | Noise analysis: path effect, run-to-run stability, competing txs context | DATA | T17, T18 | Section quantifying noise vs. rule |
| T20 | E8 Analysis | Charts: probe position vs. fee, vs. send order, per validator | DATA | T18 | PNGs in `report/figures/` regenerated by script |
| T21 | E9 Docs | README: design rationale, how each "account for" point is handled, what is NOT separated, how to run | DOC | T03, T17 | Every consideration in listing has a heading |
| T22 | E9 Docs | REPORT.md: per-validator findings, measurement dates, "what went wrong" | DOC | T19, T20 | Generated from data; all claims link to signatures |
| T23 | E9 Docs | `verify` service: re-fetch blocks for listed signatures, assert positions | DEV | T13 | Judge command reproduces all positions |
| T24 | E10 Release | Fresh-clone reproducibility test + adversarial review of design vs. judging criteria | QA | T21, T22, T23 | Clean machine run passes; review issues closed |
| T25 | E10 Release | HUMAN: submit repo link on Superteam Earn | HUMAN | T24 | Submission confirmed before deadline |

## 10. Timeline and definition of done

The schedule is tight: build and dry-run take the first 4 days, so live spending starts no later than 1 Nov.

&#91;embedded content: delivery plan · 5 phases to the deadline\]

Tasks T01–T15 map to Build and test; T16–T17 to dry-run and live; T18–T23 to analysis; T24–T25 to submit.

**Definition of done**

- [ ] ≥ 6 validators, ≥ 3 client versions, ≥ 3 runs each, all on mainnet after 21 Oct
- [ ] Total spend ≤ 0.05 SOL, shown in spend ledger
- [ ] `git clone && docker compose up` works on a clean machine in dry-run
- [ ] Every claim in REPORT.md links to signatures + slots; `verify` passes
- [ ] README covers all 7 considerations and lists what is NOT separated
- [ ] "What went wrong" section and per-measurement dates present
- [ ] Submitted on Superteam Earn before close
