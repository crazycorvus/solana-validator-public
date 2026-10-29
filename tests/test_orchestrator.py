import asyncio
import csv
import json
import time

import pytest

from fakechain import FakeChain
from itb.approval import ApprovalError, check_approval, targets_digest, write_approval
from itb.ledger import SpendLedger
from itb.orchestrator import Orchestrator, build_schedule
from itb.schedule import LeaderSchedule
from itb.sender import Sender, payer_pool

TARGETS = [{"identity": "VAL1AAAAAA", "client_family": "agave"}, {"identity": "VAL2BBBBBB", "client_family": "jito-agave"}]


def sched(n_windows=400):
    m = {}
    for k in range(n_windows):
        base = 1000 + 4 * k
        who = ["VAL1AAAAAA", "VAL2BBBBBB", "OTHER"][k % 3]
        for s in range(base, base + 4):
            m[s] = who
    return LeaderSchedule(1, 1000, m)


def make_orch(tmp_path, *, cap=10_000_000, predict=None, **kw):
    chain = FakeChain()
    sc = sched()
    chain.leader_fn = lambda s: sc.slot_leader.get(s, "OTHER")
    run = tmp_path / "run"
    led = SpendLedger(tmp_path / "ledger.json", cap)
    snd = Sender(chain, "t", run, live=True, ledger=led)
    t_base = time.time() + 0.6
    pred = predict or (lambda slot: t_base + (slot - 1000) * 0.03)
    o = Orchestrator(rpc=chain, sender=snd, sched=sc, predict=pred, payers=payer_pool(None, 8), run_dir=run,
                     lead_ms=5, prep_s=0.05, settle_s=0, min_lead_s=0.01, on_sent=lambda it, rows: chain.seal(it.window[1]), **kw)
    items, skipped = build_schedule(TARGETS, sc, 999, stamp="20261029T100000Z", seed=1, runs=1,
                                    min_ahead_slots=0, min_gap_s=4, horizon_slots=10_000)
    for it in items:  # the test clock runs ~13x faster than real slots; scale the 1.6 s spread with it
        for pr in it.batch.probes:
            pr.send_offset_ms //= 20
    return o, items, chain, led, run, skipped


def test_schedule_distinct_spaced_and_complete():
    sc = sched()
    items, skipped = build_schedule(TARGETS, sc, 999, stamp="s", seed=1, runs=2, min_ahead_slots=0,
                                    min_gap_s=4, horizon_slots=10_000)
    assert not skipped and len(items) == 2 * 2 * 7
    starts = sorted(i.window[0] for i in items)
    assert len(set(starts)) == len(starts) and all(b - a >= 2 for a, b in zip(starts, starts[1:]))
    assert all(sc.slot_leader[i.window[0]] == i.validator for i in items)
    r1 = [i.window[0] for i in items if i.validator == "VAL1AAAAAA" and i.run_id.endswith("r1")]
    r2 = [i.window[0] for i in items if i.validator == "VAL1AAAAAA" and i.run_id.endswith("r2")]
    assert min(r2) > min(r1)  # runs start at different parts of the horizon


def test_full_run_lands_and_maps(tmp_path):
    o, items, chain, led, run, _ = make_orch(tmp_path)
    stats = asyncio.run(o.run(items))
    assert stats["sent"] == len(items) == 14 and stats["stopped"] is None
    out = o.finish(tmp_path / "cache")
    assert out == {"landed": sum(len(i.batch.probes) for i in items)}
    assert led.spent == sum(i.batch.worst_case_lamports for i in items)
    lw = json.loads((run / "leader_windows.json").read_text())
    assert len(lw) == 14 and all("predicted_ts" in w for w in lw)


def test_restart_never_resends(tmp_path):
    o, items, chain, led, run, _ = make_orch(tmp_path)
    asyncio.run(o.run(items))
    spent, n_blocks = led.spent, len(chain.blocks)
    o2, items2, chain2, led2, _, _ = make_orch(tmp_path)  # same run dir + state file
    s2 = asyncio.run(o2.run(items2))
    assert s2["sent"] == 0 and s2["skipped_done"] == 14 and led2.spent == spent


def test_late_batches_are_skipped_not_sent(tmp_path):
    o, items, chain, led, run, _ = make_orch(tmp_path, predict=lambda slot: time.time() - 5)
    stats = asyncio.run(o.run(items))
    assert stats["sent"] == 0 and stats["skipped_late"] == len(items) and led.spent == 0
    assert chain.pending == []


def test_cap_stops_the_run(tmp_path):
    o, items, chain, led, run, _ = make_orch(tmp_path, cap=60_000)  # ~1.5 batches
    stats = asyncio.run(o.run(items))
    assert stats["stopped"] == "cap" and 0 < stats["sent"] < len(items)
    assert led.spent <= 60_000


def test_approval_gate(tmp_path):
    p = tmp_path / "a.json"
    with pytest.raises(ApprovalError, match="human"):
        check_approval(p, 50_000_000, TARGETS)
    write_approval(p, "a person", 50_000_000, TARGETS, 8_000_000)
    assert check_approval(p, 50_000_000, TARGETS)["approved_by"] == "a person"
    with pytest.raises(ApprovalError, match="exceeds"):
        check_approval(p, 55_000_000, TARGETS)
    with pytest.raises(ApprovalError, match="targets changed"):
        check_approval(p, 50_000_000, TARGETS[:1])
    with pytest.raises(ApprovalError):
        write_approval(p, "x", 70_000_000, TARGETS, 0)


def test_limit_items_for_a_small_test():
    from itb.orchestrator import limit_items
    items, _ = build_schedule(TARGETS, sched(), 999, stamp="s", seed=1, runs=1, min_ahead_slots=0,
                              min_gap_s=4, horizon_slots=10_000)
    one = limit_items(items, "VAL2", 2)
    assert len(one) == 2 and all(i.validator == "VAL2BBBBBB" for i in one)
    assert one == sorted(one, key=lambda i: i.window[0])
    assert limit_items(items, None, None) == items
