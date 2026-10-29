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
