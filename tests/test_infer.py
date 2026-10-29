import random

import pytest

from itb.infer import (Batch, pairwise_consistency, rank_biserial, reward_per_cu, spearman, tau_b,
                       verdict_for)
from itb.matrix import build_window_plan


def make(rule, seed, runs=3, noise=0.0, drop=0.0):
    rng = random.Random(seed)
    out = []
    for r in range(runs):
        for b in build_window_plan(f"run{r}", seed):
            ps = list(b.probes)
            if rule == "fee":
                ps.sort(key=lambda p: (-p.cu_price, p.send_order))
            elif rule == "reward":
                ps.sort(key=lambda p: (-reward_per_cu(p.cu_price, p.cu_limit), p.send_order))
            elif rule == "fifo":
                ps.sort(key=lambda p: p.send_order)
            elif rule == "random":
                rng.shuffle(ps)
            for i in range(len(ps) - 1):  # adjacent-swap noise
                if rng.random() < noise:
                    ps[i], ps[i + 1] = ps[i + 1], ps[i]
            ps = [p for p in ps if rng.random() >= drop]
            x = {"cu_price": [float(p.cu_price) for p in ps], "send_order": [float(p.send_order) for p in ps],
                 "send_offset_ms": [float(p.send_offset_ms) for p in ps],
                 "reward_per_cu": [reward_per_cu(p.cu_price, p.cu_limit) for p in ps]}
            out.append(Batch(f"run{r}", b.batch_id, b.batch_type, x, list(range(len(ps))),
                             [p.accounts_key == "shared" for p in ps]))
    return out


def v(rule, seed=1, **kw):
    return verdict_for(make(rule, seed, **kw), seed=seed, n_perm=1500, label=rule)


def test_statistics_basics():
    assert tau_b([3, 2, 1], [0, 1, 2]) == 1.0
    assert tau_b([1, 2, 3], [0, 1, 2]) == -1.0
    assert tau_b([5, 5, 5], [0, 1, 2]) is None
    assert pairwise_consistency([3, 2, 1], [0, 1, 2]) == 1.0
    assert spearman([0, 1, 2, 3], [0, 1, 2, 3]) == 1.0
    assert rank_biserial([True, True, False, False], [2, 3, 0, 1]) == 1.0


@pytest.mark.parametrize("rule,expect", [("fee", "H1"), ("reward", "H2"), ("fifo", "H3")])
def test_recovers_planted_rule(rule, expect):
    r = v(rule)
    assert r["verdict"].split(" + ")[0] == expect, r["verdict"]


def test_recovers_rule_under_noise():
    assert v("fee", noise=0.1)["verdict"].split(" + ")[0] == "H1"


def test_random_is_noise_not_a_rule():
    verdicts = [v("random", seed=s)["verdict"] for s in range(1, 13)]
    false_pos = [x for x in verdicts if x[:2] in ("H1", "H2", "H3", "H4", "H5")]
    assert len(false_pos) <= 1, verdicts  # ~ alpha-level false positives only
    assert verdicts.count("H6/noise") >= 8, verdicts


def test_insufficient_data():
    assert v("fee", runs=1, drop=0.8)["verdict"] == "insufficient data"


def test_price_and_reward_rules_are_distinguishable():
    assert v("fee")["tests"]["H2"]["passed"] is False
    assert v("reward")["tests"]["H1"]["passed"] is False


def test_deterministic():
    assert v("fee", noise=0.1) == v("fee", noise=0.1)


def make_slotted(inner, seed, runs=3, per_slot=3, use_groups=True):
    """Slots follow ARRIVAL time (earlier sends land in earlier slots, as on a real chain); `inner` decides
    the order inside a slot."""
    rng = random.Random(seed)
    out = []
    for r in range(runs):
        for b in build_window_plan(f"run{r}", seed):
            ps = sorted(b.probes, key=lambda p: p.send_order)
            slot = {p.idx: i // per_slot for i, p in enumerate(ps)}
            if inner == "fee":
                ps.sort(key=lambda p: (slot[p.idx], -p.cu_price))
            else:
                ps.sort(key=lambda p: (slot[p.idx], rng.random()))
            x = {"cu_price": [float(p.cu_price) for p in ps], "send_order": [float(p.send_order) for p in ps],
                 "send_offset_ms": [float(p.send_offset_ms) for p in ps],
                 "reward_per_cu": [reward_per_cu(p.cu_price, p.cu_limit) for p in ps]}
            out.append(Batch(f"run{r}", b.batch_id, b.batch_type, x, list(range(len(ps))),
                             [p.accounts_key == "shared" for p in ps], "",
                             [slot[p.idx] for p in ps] if use_groups else None))
    return out


def test_cross_slot_arrival_order_is_not_mistaken_for_fifo():
    """Random order inside every slot, slots in arrival order: no intra-block rule. Pooling across slots
    would report a strong FIFO effect; comparing only same-slot pairs must not."""
    naive = verdict_for(make_slotted("random", 1, use_groups=False), seed=1, n_perm=800, label="naive")
    proper = verdict_for(make_slotted("random", 1), seed=1, n_perm=800, label="proper")
    assert naive["tests"]["H3"]["effect"] > 0.5            # the confound is real
    assert not proper["tests"]["H3"]["passed"] and abs(proper["tests"]["H3"]["effect"]) < 0.3
    assert proper["verdict"].split(" + ")[0] not in ("H1", "H2", "H3")


def test_fee_rule_still_found_inside_slots():
    r = verdict_for(make_slotted("fee", 2), seed=2, n_perm=800, label="x")
    assert r["verdict"].split(" + ")[0] == "H1", r["verdict"]


def test_no_same_slot_pairs_is_insufficient_not_noise():
    bs = make_slotted("random", 3, per_slot=1)  # every probe alone in its slot
    assert verdict_for(bs, seed=3, n_perm=200, label="x")["verdict"] == "insufficient data"
