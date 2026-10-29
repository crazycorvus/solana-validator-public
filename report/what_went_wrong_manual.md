<!-- Human-curated incident log. The generator appends data-derived problems after these entries. -->
- **2026-10-28 · Reward-per-CU batch could not separate H1 from H2.** The first design held the total priority
  fee fixed, which made price order and reward-per-CU order identical. Caught in review before any live run;
  redesigned so the two rules give opposite orders (`docs/DESIGN.md`).
- **2026-10-28 · Every probe would have failed with a 10 000 CU limit.** The memo instruction alone consumes
  about 19.4k CU. Found by the localnet end-to-end test (probes landed with `ProgramFailedToComplete`); limits
  are now at least 30 000 and a unit test guards the floor.
- **2026-10-28 · Mainnet blocks contain version-1 transactions.** `getBlock` with
  `maxSupportedTransactionVersion: 0` is rejected; the collector now asks for version 1.
- **2026-10-28 · Window-start prediction is noisy on Alchemy.** Hit rates within one slot over four runs were
  91 %, 99 %, 71 % and 65 %; websocket notification jitter (250-420 ms) varies from run to run and
  `slotsUpdatesSubscribe` is not offered. We therefore judge delivery by where probes actually landed, not by
  how well we predicted the window.
- **2026-10-28 · Client family cannot be read from `getClusterNodes`.** Jito, BAM and Harmonic builds share
  Agave's version string. An early classifier matched names loosely and filed `HarmonicAgave` as plain Agave;
  it now matches validators.app `software_client` values exactly.
- **2026-10-28 · First capped live test (2 batches, plain Agave validator `7QQGNm3p`, RPC path via Alchemy,
  run `20261028T190704Z`, 73 728 lamports spent).** Batch 1 (8 probes) was sent 827 ms after the real window
  start because the slot-clock fit was too short on its first window (777 ms prediction error; the second
  window was off by 21 ms); all 8 landed, in slot +5 on the *next* leader (`HEL1USMZ`), so none counted.
  Batch 2 (6 probes) was sent 71 ms after the real window start, was accepted by the RPC provider without
  error, and **never landed anywhere** (`getSignatureStatuses` with history: null for all 6). Two batches on
  one validator are not enough to explain it; candidates are the leader dropping RPC-forwarded packets
  (stake-weighted QoS) or the provider forwarding only to later leaders. Next test targets the validator that
  did accept the late batch.
- **2026-10-28 · Mainnet slots are ~214 ms, not 400 ms.** Two windows 2 828 slots apart were 606 s apart. A
  4-slot window is therefore ~0.86 s; constants that assumed 400 ms (timing-spread of 1.2 s, gap and horizon
  conversions, clock warm-up of 25 slots) were wrong and now derive from the measured slot time.
- **2026-10-28 · The landing mapper called late/early probes `dropped`.** It only looked at the target
  window +/-1 slot, so the 8 probes that landed 5 slots late were reported as dropped. It now looks up where
  every unseen probe landed (`getSignatureStatuses`) and collects those slots too.
- **2026-10-29 · The first full live study silently stopped landing after about a quarter of its batches
  (run `20261029T084255Z`).** My payer funding assumed a rent-exempt minimum of 890 880 lamports; on this
  network it is 650 240 (`getMinimumBalanceForRentExemption(0)`), so each payer had only ~290 000 lamports of
  fee money (~57 transactions) for a schedule needing ~1 100 000. After that the RPC provider kept accepting
  our sends but the leader rejected every transaction for lack of fee funds: 74 % of 1 704 probes never
  reached the chain, uniformly across all validators, which is what exposed it as ours and not theirs.
  Partial batches lost their highest-index probes (the payers that ran dry first), which would have biased the
  ordering tests. Fix: deposits are now rent floor + each payer's worst-case fees for the whole schedule (unit
  test), and the 166 affected batches were re-sent (run `20261029T123727Z`, 99 % of probes on chain). The
  failed batches are kept as evidence and marked `exclusions.json`; they are excluded from delivery statistics
  and from the analysis. Fees paid on chain for the whole project: 9 101 670 lamports (0.0091 SOL), plus ~0.0003 SOL of funding and sweep transaction fees.
- **2026-10-29 · The deposit sweep failed twice.** A fee payer cannot be drained to zero by paying its own fee
  (it must stay rent-exempt). Deposits were recovered by letting the main wallet pay the fee and the payer only
  sign as source; the live run now sweeps this way and retries.
- **2026-10-29 · The ledger over-counted spend.** It reserves the worst case per batch, so the 166 rejected
  batches counted as spent although their fees were never paid (8.9M reserved vs 2.46M paid on chain). A human
  reconciled it to on-chain fees (`itb reconcile`, interactive, audit trail kept, cap unchanged).
- **2026-10-29 · The pre-registered H5 test was trivially satisfied.** Position vs send offset over all
  probes gave +0.82..+0.99 on 7 of 8 validators because later sends land in later slots whatever the leader
  does. Found only after seeing the data; changed to same-slot pairs and disclosed in `docs/DESIGN.md`
  (Amendments). Result: the same-slot version still passes for 7 of 8, so it is a real effect.
- **2026-10-29 · `rank_in_batch` was computed across all batches sharing a batch id.** `verify` caught it
  (claimed ranks 8..15 against recomputed 0..7). The analysis was unaffected (it uses only relative order
  inside a validator/run/batch and re-derived identically) but the committed column was wrong; fixed and
  regenerated.
- **2026-10-29 · Analysis crashed on real data** in a verdict path (modifier-only) that synthetic data never
  reached; fixed with a test.
