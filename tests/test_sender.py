import asyncio
import json
import time

import pytest
from solders.hash import Hash

from itb.ledger import CapExceeded, SpendLedger
from itb.matrix import build_window_plan
from itb.sender import DryTransport, RpcTransport, Sender, payer_pool, prepare


class Recorder:
    path, live = "rec", True

    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(raw)
        return {"result": "sig"}

    async def close(self):
        pass


def batch(kind="fee_ladder"):
    return next(b for b in build_window_plan("t", 1) if b.batch_type == kind)


def test_dry_run_sends_nothing_and_refuses_live_transport(tmp_path):
    b = batch()
    prep = prepare(b, payer_pool(None, len(b.probes)), Hash.default())
    s = Sender(DryTransport(), "t", tmp_path, live=False)
    rows = asyncio.run(s.send_batch(prep, time.time()))
    assert len(rows) == len(b.probes) and all(r["path"] == "dry" for r in rows)
    with pytest.raises(RuntimeError):
        Sender(RpcTransport("http://x"), "t", tmp_path, live=False)
    with pytest.raises(RuntimeError):
        Sender(Recorder(), "t", tmp_path, live=True)  # no ledger


def test_live_respects_send_order_and_reserves_before_send(tmp_path):
    b = batch()
    prep = prepare(b, payer_pool(None, len(b.probes)), Hash.default())
    led = SpendLedger(tmp_path / "l.json", 1_000_000)
    rec = Recorder()
    s = Sender(rec, "t", tmp_path, live=True, ledger=led)
    asyncio.run(s.send_batch(prep, time.time()))
    order = [p.raw for p in sorted(prep, key=lambda p: p.spec.send_order)]
    assert rec.sent == order
    assert led.spent == b.worst_case_lamports > 0  # whole batch reserved up front


def test_live_refuses_whole_batch_over_cap(tmp_path):
    b = batch()
    prep = prepare(b, payer_pool(None, len(b.probes)), Hash.default())
    led = SpendLedger(tmp_path / "l.json", 10_000)  # < worst case of 8 probes
    rec = Recorder()
    s = Sender(rec, "t", tmp_path, live=True, ledger=led)
    with pytest.raises(CapExceeded):
        asyncio.run(s.send_batch(prep, time.time()))
    assert rec.sent == [] and led.spent == 0


def test_timing_spread_offsets_respected(tmp_path):
    b = batch("timing_spread")
    prep = prepare(b, payer_pool(None, len(b.probes)), Hash.default())
    s = Sender(DryTransport(), "t", tmp_path, live=False)
    rows = asyncio.run(s.send_batch(prep, time.time() + 0.05))
    ts = sorted(r["send_ts_us"] for r in rows)
    assert (ts[-1] - ts[0]) / 1000 >= 0.8 * max(p.spec.send_offset_ms for p in prep)


def test_payer_pool_deterministic_and_distinct():
    a = [str(k.pubkey()) for k in payer_pool(None, 5)]
    assert a == [str(k.pubkey()) for k in payer_pool(None, 5)] and len(set(a)) == 5
