# Inside the Block: report

No post-Alpenglow measurement data yet: this report is empty by design.

Regenerate with `itb analyze && itb report`. Every claim below is followed by the signatures and slots it rests on; `docker compose run verify` re-checks them against the chain.

## Summary

| Validator | Client (at selection) | Runs | Probes | Landed in window | Verdict | Confidence |
|---|---|---|---|---|---|---|

## Measurement log

| Run | Date (UTC) | Provider | Clock offset (s) | Live | Probes |
|---|---|---|---|---|---|

## Findings per validator

## What went wrong

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

- No measurements yet, so nothing to check.

## Limits

See README, *What this design does NOT separate*: arrival-time jitter, provider forwarding path, Jito block-engine vs banking stage, entry boundaries, small samples.
