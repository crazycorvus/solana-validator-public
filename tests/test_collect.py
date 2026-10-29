import csv
import json

from itb.collect import map_landings, slots_for_run


def mk(tmp, probes, blocks, leaders):
    run = tmp / "run"
    (run / "blocks").mkdir(parents=True)
    for slot, sigs in blocks.items():
        txs = [{"transaction": {"signatures": [s]}, "meta": {"err": None if s != "BAD" else {"E": 1}}}
               for s in sigs]
        (run / "blocks" / f"{slot}.json").write_text(json.dumps({"slot": slot, "transactions": txs}))
    (run / "leaders.json").write_text(json.dumps({str(k): v for k, v in leaders.items()}))
    with open(run / "probes.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["probe_id", "batch_id", "signature", "leader", "window_start"])
        w.writeheader()
        w.writerows(probes)
    return run


def p(i, sig, leader="L", win=100):
    return {"probe_id": f"probe:r:b1:{i}", "batch_id": "b1", "signature": sig, "leader": leader,
            "window_start": win}


def test_outcomes_and_ranks(tmp_path):
    leaders = {100: "L", 101: "L", 102: "L", 103: "L", 104: "M", 99: "M"}
    blocks = {100: ["vote", "a", "b"], 101: ["c"], 104: ["d"], 99: ["e"], 105: []}
    probes = [p(0, "b"), p(1, "a"), p(2, "c"), p(3, "d"), p(4, "missing"), p(5, "BAD"), p(6, "e")]
    run = mk(tmp_path, probes, blocks, leaders)
    rows = {r["signature"]: r for r in map_landings(run / "probes.csv", run)}
    assert rows["a"]["tx_index_in_block"] == 1 and rows["b"]["tx_index_in_block"] == 2
    assert rows["a"]["rank_in_batch"] == 0 and rows["b"]["rank_in_batch"] == 1 and rows["c"]["rank_in_batch"] == 2
    assert rows["d"]["outcome"] == "landed_other_leader"
    assert rows["e"]["outcome"] == "landed_other_leader"
    assert rows["missing"]["outcome"] == "dropped"
    assert rows["BAD"]["outcome"] == "dropped"  # not in any block -> dropped
    assert (run / "landings.csv").exists()


def test_failed_tx_flagged_and_outside_window(tmp_path):
    leaders = {100: "L", 101: "L", 102: "L", 103: "L", 104: "L"}
    run = mk(tmp_path, [p(0, "BAD"), p(1, "z")], {100: ["BAD"], 104: ["z"]}, leaders)
    rows = {r["signature"]: r for r in map_landings(run / "probes.csv", run)}
    assert rows["BAD"]["status"] == "failed" and rows["BAD"]["outcome"] == "landed"
    assert rows["z"]["outcome"] == "landed_outside_window"


def test_slots_for_run(tmp_path):
    run = mk(tmp_path, [p(0, "a", win=100)], {}, {})
    assert slots_for_run(run / "probes.csv") == list(range(99, 105))


def _b58enc(b: bytes) -> str:
    from itb.collect import _B58
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + out


def _json_tx(tx):
    m = tx.message
    return {"transaction": {"signatures": [str(tx.signatures[0])], "message": {
        "accountKeys": [str(k) for k in m.account_keys],
        "instructions": [{"programIdIndex": i.program_id_index, "data": _b58enc(bytes(i.data))}
                         for i in m.instructions]}}, "meta": {"err": None, "fee": 5000,
                                                              "computeUnitsConsumed": 300}}


def test_compact_roundtrip_and_full_vs_compact_split(tmp_path):
    from solders.hash import Hash

    from itb.collect import compact_blocks, compute_budget
    from itb.matrix import build_window_plan
    from itb.sender import payer_pool, prepare
    b = build_window_plan("t", 1)[0]
    prep = prepare(b, payer_pool(None, len(b.probes)), Hash.default())
    jt = [_json_tx(p.tx) for p in prep]
    for p, t in zip(prep, jt):
        assert compute_budget(t) == (p.spec.cu_price, p.spec.cu_limit)
    cache, run = tmp_path / "cache", tmp_path / "run"
    (cache / "blocks").mkdir(parents=True)
    other = {"transaction": {"signatures": ["zzz"], "message": {"accountKeys": ["a"], "instructions": []}},
             "meta": {"err": None}}
    (cache / "blocks" / "100.json").write_text(json.dumps({"slot": 100, "transactions": [other, jt[0]]}))
    (cache / "blocks" / "101.json").write_text(json.dumps({"slot": 101, "transactions": [other]}))
    res = compact_blocks(cache, run, {prep[0].signature})
    assert res == {"full": 0, "compact": 2}                      # default: everything compact and gzipped
    res = compact_blocks(cache, run, {prep[0].signature}, keep_full=True)
    assert res == {"full": 1, "compact": 1}                      # opt-in: full block only where a probe landed
    assert (run / "blocks" / "100.json.gz").exists() and (run / "blocks" / "101.json.gz").exists()
    assert not (run / "blocks" / "101.json").exists()
    from itb.collect import block_index
    idx = block_index(run / "blocks")
    assert idx[prep[0].signature][:2] == (100, 1) and idx["zzz"][:2] == (101, 0)


def test_rank_is_per_run_and_batch_not_per_batch_id(tmp_path):
    """Regression (found by `verify` on the first full study): ranks were computed across every batch that
    shared a batch id, so two runs' b1 batches were ranked together."""
    leaders = {100: "L", 101: "L", 102: "L", 103: "L"}
    run = mk(tmp_path, [], {100: ["a1", "b1", "a2", "b2"]}, leaders)
    rows = []
    for sig, rid in (("a1", "RUN_A"), ("a2", "RUN_A"), ("b1", "RUN_B"), ("b2", "RUN_B")):
        rows.append({"probe_id": f"probe:{rid}:b1:0", "batch_id": "b1", "signature": sig, "leader": "L",
                     "window_start": 100, "run_id": rid})
    with open(run / "probes.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    out = {r["signature"]: r["rank_in_batch"] for r in map_landings(run / "probes.csv", run)}
    assert out == {"a1": 0, "a2": 1, "b1": 0, "b2": 1}
