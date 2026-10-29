# Pre-registered experiment design (T03)

Written and committed BEFORE any live run. Changes after the first live run go in a dated
"Amendments" section at the bottom; they are never edited in place.

All data: mainnet, after Alpenglow activation (2026-10-21). Unit of analysis = one batch sent to
one leader window (4 consecutive slots of one validator). Position = rank of a probe among OUR
probes in the same block, ordered by (slot, tx index). Probes landing in a slot of a different
leader, or split across slots, are excluded and counted.

| ID | Hypothesis | Batch | Variable | Statistic | Decision rule (per validator, pooled over >=3 runs) |
|----|-----------|-------|----------|-----------|------------------------------------------------------|
| H1 | Fee-per-CU ordering | b1, b3a/b3b | cu_price | Kendall tau(position, -price); pairwise consistency; permutation p (10k perms, seeded) | tau <= -0.6 and p < 0.01 and consistent in >= 2/3 runs and in b3a AND b3b |
| H2 | Reward-per-CU (fee / requested CU) | b2 | cu_limit at fixed total fee | Kendall tau(position, -price/CU) | same thresholds; H2 over H1 only if b2 supports it and b1 effect is also explained by price/CU |
| H3 | Arrival order (FIFO) | b1, b3a/b3b | send_order | Kendall tau(position, send_order) | tau >= 0.6, p < 0.01, holds when fee is reversed (b3a vs b3b) |
| H4 | Account-lock conflicts | b4 | shared vs disjoint writable account | Mann-Whitney on position of shared set vs disjoint; shared-set internal order vs send order | p < 0.01 and effect size (rank-biserial) >= 0.5 |
| H5 | Batch / entry clustering | b5 | send offset in window | Spearman(position, offset); share of probes per entry; within-entry order vs fee | rho >= 0.6 with p < 0.01, or entry-cluster membership explained by offset |
| H6 | No stable rule (null) | b6 | none (identical txs, shuffled send) | Kendall tau vs every variable; run-to-run rank correlation | Declared if no H1-H5 rule passes AND control tau vs each variable has |tau| < 0.3 |

Rules shared by all tests
* Verdict set: `H1..H5` (strict), `mixed` (a rule holds with 0.3 <= |tau| < 0.6), `H6/noise`, `insufficient data` (< 16 landed probes in same-leader slots).
* Effect size is always reported next to p. No multiple-comparison hiding: 5 tests per validator, Holm correction reported.
* Reported confidence = share of runs agreeing with the verdict.
* Order within one entry is read from entry boundaries where available (`getBlock` has no entry index; we reconstruct from the PoH-free tx order and flag it as a limitation).

Sample size
* 7 batches x 6-8 probes per run, 3 runs per validator, >= 6 validators: ~970 txs, worst-case ~0.005 SOL at the default ladder (`itb plan`). Remaining budget is reserved for more repeats on validators with ambiguous verdicts (decided by the analysis, not by hand).

Path comparison
* Primary: direct TPU/QUIC to the current leader. Comparison: RPC `sendTransaction`. The same batch type is sent via both paths in different runs; path effect = difference in landing rate and rank correlation between paths.

Not separated (stated in README too)
* QUIC stream jitter vs scheduler thread order; stake-weighted QoS effects on our connection; Jito block-engine path vs native banking stage when both are active.

## Revisions before the first live run (in place, dated 2026-10-28)
* b2 (reward-per-CU) redesigned: the first version held total priority fee fixed, which made price order and
  reward-per-CU order identical, so H1 and H2 could not be told apart. Now price rises with CU limit while
  (priority fee + base_fee/2) / limit falls, so the two rules give opposite orders. H1 and H2 are both
  evaluated over b1, b2, b3a, b3b; b2 is the decisive batch.
* CU limits raised to >= 30 000 (b2 spans 30 000-200 000): found on a real validator, where the memo ix alone
  uses ~19.4k CU and a 10k limit made every probe fail. All probes must succeed so a failed tx never masquerades
  as an ordering datum.
* Send timing: the localnet orchestrator test showed that sending 100 ms BEFORE the predicted window start put
  31 of 54 probes in the previous slot (on mainnet: another leader, excluded); sending 50 ms AFTER it put all 54
  inside the window (prediction error there was <= 12 ms). Default `send_lead_ms` is now -50, and b5 spreads its
  probes over 1.2 s instead of 1.6 s so the last one cannot spill past the window. Mainnet jitter is much larger
  than localnet's; the first capped live test exists to tune this value before the full runs.
* Position is compared only between probes that landed in the SAME slot (found in the T24 review). Across slots the
  order is just arrival time, so pooling all slots of a window made a rule-free leader look FIFO (naive H3 effect
  > 0.5 on synthetic random-within-slot data) and could hide a fee rule. Tau, pairwise consistency, the H4 effect
  and every permutation now use same-slot pairs only; H5 (arrival time) deliberately stays cross-slot. A validator
  with fewer than 30 same-slot pairs is reported as `insufficient data`, never as noise.
* Mainnet slot time is ~0.214 s (measured 2026-10-28), not 0.4 s: a 4-slot window is ~0.86 s. Gap, horizon and
  warm-up conversions now use the measured slot time; b5 spreads its probes over 60 % of the window
  (~515 ms). The slot clock is accurate only to ~400-470 ms median on Alchemy (see README), which is the main
  open risk for hitting a window.
* H5 is reduced: entry/micro-block clustering needs entry boundaries, which `getBlock` does not return. The
  H5 statistic (Spearman of position vs send offset in b5, identical fees) is reported as "arrival-order
  tie-break among equal fees", not as batching.
* Verdict: exactly one of H1/H2/H3 passing gives that verdict; several give `ambiguous`; H4 and the tie-break
  are appended as modifiers. A pass additionally needs the pooled effect >= 0.6, p < 0.01 (one-sided; H4
  two-sided), >= 2/3 of runs individually >= 0.6, and every batch type >= 0.3 (a contradicting batch vetoes).
* Validated on synthetic chains with a planted rule (tests/test_infer.py): recovers H1/H2/H3, also with 10%
  adjacent-swap noise; on random order 38 of 40 seeds give `H6/noise`, 2 give `mixed`, 0 false rules.

## Amendments (made AFTER seeing live data; dated 2026-10-29)
1. **H5 was trivially satisfied and is now same-slot only.** The pre-registered H5 statistic was Spearman of
   position vs send offset over ALL probes of the b5 batch. On the first full live data it came out at +0.82 to
   +0.99 for 7 of 8 validators, because probes sent later land in later slots whatever rule the leader uses
   (cross-slot order is just time). That is a flaw in the pre-registered test, not a finding. H5 is now Kendall
   tau between earlier send and earlier position over same-slot pairs only, like H1-H4, with the same thresholds.
   Both versions were run; the original numbers are kept in `data/analysis/verdicts_h5_crossslot.json`.
2. **Redo of the first live study** (payer deposits too small: see README and report). Batches that never fully
   reached the chain were excluded (`exclusions.json`) and re-sent under the same run ids.
