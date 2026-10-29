import csv

from test_pipeline import run_pipeline
from itb.verify import verify_run


def rewrite(path, fn):
    rows = list(csv.DictReader(open(path)))
    fn(rows)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def setup(tmp_path):
    # run_pipeline leaves the chain behind only inside; rebuild it the same way and keep a handle
    import test_pipeline as tp
    from fakechain import FakeChain
    chains = []
    orig = FakeChain.__init__

    def spy(self, *a, **k):
        orig(self, *a, **k)
        chains.append(self)
    FakeChain.__init__ = spy
    try:
        b, rows, res, led = run_pipeline(tmp_path, "fee")
    finally:
        FakeChain.__init__ = orig
    return chains[0], tmp_path / "run"


def test_verify_passes_on_honest_data(tmp_path):
    chain, run = setup(tmp_path)
    rep = verify_run(chain, run)
    assert rep["ok"] and rep["checked"]["landed"] == 8


def test_verify_catches_tampered_position_leader_and_drop(tmp_path):
    chain, run = setup(tmp_path)

    def tamper(rows):
        rows[0]["tx_index_in_block"] = "7" if rows[0]["tx_index_in_block"] != "7" else "6"
        rows[1]["leader"] = "SOMEONE_ELSE"
        rows[2].update(outcome="dropped", slot="", leader="", tx_index_in_block="", rank_in_batch="")
        rows[3]["rank_in_batch"] = "99"
    rewrite(run / "landings.csv", tamper)
    rep = verify_run(chain, run)
    assert not rep["ok"]
    kinds = " | ".join(p["problem"] for p in rep["problems"])
    assert "claimed index" in kinds and "leader on chain" in kinds and "claimed dropped" in kinds
    assert "rank_in_batch" in kinds
