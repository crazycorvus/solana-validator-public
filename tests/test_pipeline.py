"""Offline end-to-end: send -> collect -> compact -> map, against a chain with a KNOWN ordering rule."""
import asyncio
import csv
import time

from solders.hash import Hash

from fakechain import FakeChain
from itb.collect import collect_slots, compact_blocks, map_landings
from itb.ledger import SpendLedger
from itb.matrix import build_window_plan
from itb.sender import Sender, payer_pool, prepare


def run_pipeline(tmp_path, rule):
    b = next(x for x in build_window_plan("e2e", 3) if x.batch_type == "fee_ladder")
    prep = prepare(b, payer_pool(None, len(b.probes)), Hash.default())
    chain = FakeChain(rule=rule)
    led = SpendLedger(tmp_path / "ledger.json", 1_000_000)
    run = tmp_path / "run"
    s = Sender(chain, "e2e", run, live=True, ledger=led)
    asyncio.run(s.send_batch(prep, time.time(), leader="LEADER", window=[200, 201, 202, 203]))
    chain.seal(201)
    collect_slots(chain, list(range(199, 205)), run, tmp_path / "cache")
    with open(run / "probes.csv") as f:
        sigs = {r["signature"] for r in csv.DictReader(f)}
    res = compact_blocks(tmp_path / "cache", run, sigs)
    rows = map_landings(run / "probes.csv", run)
    return b, rows, res, led


def test_fee_rule_recovered_from_chain(tmp_path):
    b, rows, res, led = run_pipeline(tmp_path, "fee")
    assert res == {"full": 0, "compact": 6}  # all compact by default; 5 slots are not on the chain (stored as skipped)
    assert all(r["outcome"] == "landed" for r in rows) and len(rows) == len(b.probes)
    price = {p.probe_id: p.cu_price for p in b.probes}
    by_rank = sorted(rows, key=lambda r: r["rank_in_batch"])
    prices = [price[r["probe_id"]] for r in by_rank]
    assert prices == sorted(prices, reverse=True)
    assert led.spent == b.worst_case_lamports


def test_reverse_rule_differs(tmp_path):
    b, rows, _, _ = run_pipeline(tmp_path, "reverse")
    order = {p.probe_id: p.send_order for p in b.probes}
    ranked = [order[r["probe_id"]] for r in sorted(rows, key=lambda r: r["rank_in_batch"])]
    assert ranked == sorted(ranked, reverse=True)


def test_probe_landing_outside_collected_window_is_found_not_called_dropped(tmp_path):
    """Regression from the first live test: all 8 probes landed 5 slots after the window start, outside
    the +/-1 slots the collector fetched, and were mislabelled `dropped`."""
    from itb.collect import collect_run
    b = next(x for x in build_window_plan("e2e", 3) if x.batch_type == "fee_ladder")
    prep = prepare(b, payer_pool(None, len(b.probes)), Hash.default())
    chain = FakeChain(rule="fee")
    chain.leader_fn = lambda s: "LEADER"
    run = tmp_path / "run"
    led = SpendLedger(tmp_path / "ledger.json", 1_000_000)
    s = Sender(chain, "e2e", run, live=True, ledger=led)
    asyncio.run(s.send_batch(prep, time.time(), leader="LEADER", window=[200, 201, 202, 203]))
    chain.seal(207)  # window + 7: far outside 199..204
    out = collect_run(chain, run, tmp_path / "cache")
    assert out == {"landed_outside_window": 8}, out
    # a probe the chain never saw stays dropped
    with open(run / "probes.csv") as f:
        rows = list(csv.DictReader(f))
    rows.append({**rows[0], "signature": "NEVERSEEN", "probe_id": "probe:x"})
    with open(run / "probes.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    out = collect_run(chain, run, tmp_path / "cache")
    assert out["dropped"] == 1 and out["landed_outside_window"] == 8
