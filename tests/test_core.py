import json
import random

import pytest
from solders.hash import Hash
from solders.keypair import Keypair

from itb.config import ConfigError, load_settings
from itb.discover import classify_client
from itb.ledger import CapExceeded, SpendLedger
from itb.matrix import build_window_plan, estimate_cost
from itb.probes import build_tx, fee_lamports, signature_of


def test_ledger_refuses_at_cap_and_survives_restart(tmp_path):
    p = tmp_path / "l.json"
    led = SpendLedger(p, 10_000)
    led.reserve(6_000, "r1")
    led2 = SpendLedger(p, 10_000)  # restart
    assert led2.spent == 6_000
    with pytest.raises(CapExceeded):
        led2.reserve(4_001, "r1")
    led2.reserve(4_000, "r1")
    assert led2.remaining() == 0


def test_cap_only_ratchets_down(tmp_path):
    p = tmp_path / "l.json"
    SpendLedger(p, 10_000)
    assert SpendLedger(p, 50_000).cap == 10_000


def test_ledger_rejects_cap_above_ceiling(tmp_path):
    with pytest.raises(CapExceeded):
        SpendLedger(tmp_path / "l.json", 70_000_000)


def test_live_requires_flags_and_key():
    with pytest.raises(ConfigError):
        load_settings(live=True, env={})
    with pytest.raises(ConfigError):
        load_settings(live=True, cap_sol=0.05, env={})
    with pytest.raises(ConfigError):
        load_settings(cap_sol=0.05, env={})
    with pytest.raises(ConfigError):
        load_settings(live=True, cap_sol=0.5, env={"SOLANA_KEYPAIR": "x"})
    s = load_settings(live=True, cap_sol=0.05, env={"SOLANA_KEYPAIR": "x"})
    assert s.cap_lamports == 50_000_000
    assert load_settings(env={}).live is False


def test_fee_math():
    assert fee_lamports(0, 10_000) == 5_000
    assert fee_lamports(1_000, 10_000) == 5_010


def test_build_tx_has_memo_and_unique_sig():
    plan = build_window_plan("t", 1)
    b = plan[0]
    sigs = set()
    for spec in b.probes:
        kp = Keypair()
        tx = build_tx(spec, kp, Hash.default())
        sigs.add(signature_of(tx))
        assert spec.probe_id.encode() in bytes(tx.message.instructions[3].data)
    assert len(sigs) == len(b.probes)


def test_matrix_deterministic_and_fits_budget():
    a = [[p.send_order for p in b.probes] for b in build_window_plan("r", 7)]
    b = [[p.send_order for p in b.probes] for b in build_window_plan("r", 7)]
    assert a == b
    est = estimate_cost(runs=3, validators=6)
    assert est["total_lamports"] < 50_000_000


def test_reward_per_cu_batch_separates_price_from_reward():
    from itb.infer import reward_per_cu
    b = [x for x in build_window_plan("r", 1) if x.batch_type == "reward_per_cu"][0]
    by_price = sorted(b.probes, key=lambda p: -p.cu_price)
    by_reward = sorted(b.probes, key=lambda p: -reward_per_cu(p.cu_price, p.cu_limit))
    assert [p.idx for p in by_price] == [p.idx for p in by_reward][::-1]


def test_classify():
    assert classify_client("4.3.2", "JitoLabs") == "jito-agave"
    assert classify_client("4.3.2", "AgaveBam") == "agave-bam"
    assert classify_client("4.3.2", "HarmonicAgave") == "harmonic-agave"  # not plain agave
    assert classify_client("26.10.4", "FireBAM") == "firedancer-bam"
    assert classify_client("4.3.2", "Rakurai") == "other:rakurai"
    assert classify_client("0.811.30108") == "firedancer-family-unverified"
    assert classify_client("4.3.2") == "unverified"


def test_every_probe_requests_enough_cu_for_the_memo_program():
    from itb.matrix import MIN_CU_LIMIT
    for b in build_window_plan("r", 1):
        assert all(p.cu_limit >= MIN_CU_LIMIT for p in b.probes), b.batch_id
