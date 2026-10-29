import csv
import json
import random

from itb.collect import compact_block
from itb.infer import Batch
from itb.noise import competition_context, landing_stats, null_sd, path_effect, run_stability
from test_infer import make


def tag(batches, path, runs=None):
    for b in batches:
        b.path = path if runs is None or b.run_id in runs else b.path
    return batches


def test_path_effect_detects_a_path_that_destroys_the_rule():
    good = tag(make("fee", 1, runs=4), "tpu_quic")
    bad = tag(make("random", 2, runs=4), "rpc")
    for b in bad:
        b.run_id += "-rpc"
    r = path_effect(good + bad, "H1", "s", n_perm=1500)
    assert abs(r["difference"]) > 0.5 and r["p"] < 0.01


def test_path_effect_null_when_both_paths_follow_the_rule():
    a = tag(make("fee", 1, runs=3), "tpu_quic")
    b = tag(make("fee", 2, runs=3), "rpc")
    for x in b:
        x.run_id += "-rpc"
    r = path_effect(a + b, "H1", "s", n_perm=1500)
    assert abs(r["difference"]) < 0.1 and r["p"] > 0.05


def test_path_effect_needs_two_real_paths():
    assert path_effect(tag(make("fee", 1), "dry"), "H1", "s") is None


def test_run_stability_flags_unstable_validator():
    stable = run_stability(make("fee", 1, runs=3), "H1")
    assert stable["sign_consistent"] and stable["sd"] < 0.05
    bs = make("fee", 1, runs=2) + tag(make("random", 3, runs=1), "x")
    for b in bs[-7:]:
        b.run_id = "run9"
    unstable = run_stability(bs, "H1")
    assert unstable["sd"] > 0.3


def test_null_sd_small_but_nonzero_and_effect_far_above_it():
    bs = make("fee", 1, runs=3)
    n = null_sd(bs, "H1", "s", 800)
    assert 0 < n < 0.15


def test_landing_stats(tmp_path):
    run = tmp_path / "r"
    run.mkdir()
    with open(run / "probes.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["signature", "leader", "path", "batch_type"])
        w.writeheader()
        for i in range(4):
            w.writerow({"signature": f"s{i}", "leader": "L", "path": "rpc", "batch_type": "control"})
    with open(run / "landings.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["signature", "outcome"])
        w.writeheader()
        for i, o in enumerate(["landed", "landed", "dropped", "landed_other_leader"]):
            w.writerow({"signature": f"s{i}", "outcome": o})
    st = landing_stats([run])["L|rpc"]["_all"]
    assert st["n"] == 4 and st["landing_rate"] == 0.5 and st["dropped"] == 1


def block(slot, sorted_by_price, rng):
    prices = [rng.choice([0, 0, 100, 1000, 5000, 20000]) for _ in range(120)]
    if sorted_by_price:
        prices.sort(reverse=True)
    txs = [{"sig": f"{slot}-{i}", "vote": i % 7 == 0, "err": None, "fee": 5000, "cu_used": 1,
            "cu_price": p, "cu_limit": 1} for i, p in enumerate(prices)]
    txs.append({"sig": "ours", "vote": False, "err": None, "fee": 1, "cu_used": 1, "cu_price": 1, "cu_limit": 1})
    return {"slot": slot, "compact": True, "transactions": txs}


def ctx_run(tmp_path, sorted_by_price):
    run = tmp_path / ("a" if sorted_by_price else "b")
    (run / "blocks").mkdir(parents=True)
    rng = random.Random(4)
    for slot in (10, 11):
        (run / "blocks" / f"{slot}.json").write_text(json.dumps(block(slot, sorted_by_price, rng)))
    (run / "leaders.json").write_text(json.dumps({"10": "L", "11": "L"}))
    open(run / "probes.csv", "w").write("signature\nours\n")
    open(run / "landings.csv", "w").write("signature,slot\nours,10\n")
    return run


def test_competition_context_separates_sorted_from_unsorted_blocks(tmp_path):
    hi = competition_context([ctx_run(tmp_path, True)])["L"]
    lo = competition_context([ctx_run(tmp_path, False)])["L"]
    assert hi["n_blocks"] == 1  # only the block containing our probe
    assert hi["context_tau_mean"] > 0.8 and abs(lo["context_tau_mean"]) < 0.3


def test_compact_block_flags_votes():
    full = {"slot": 1, "transactions": [
        {"transaction": {"signatures": ["v"], "message": {"accountKeys": ["a", "Vote111111111111111111111111111111111111111"],
                                                          "instructions": [{"programIdIndex": 1, "data": "1"}]}}, "meta": {}},
        {"transaction": {"signatures": ["t"], "message": {"accountKeys": ["a"], "instructions": []}}, "meta": {}}]}
    assert [t["vote"] for t in compact_block(full)["transactions"]] == [True, False]
