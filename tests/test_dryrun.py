import random

from itb.dryrun import LEAD_GRID_MS, WindowDryRun, format_report
from itb.schedule import SlotWatcher
from itb.snapshot import snapshot_targets


def replay(jitter, n=900, seed=1, slot_s=0.214):
    rng = random.Random(seed)
    w = SlotWatcher("http://unused")
    d = WindowDryRun(w, prep_s=10)
    w.listeners.append(d.on_slot)
    for i in range(n):
        w.ingest(5000 + i, 100 + i * slot_s + rng.uniform(-jitter, jitter))
    return d


def test_perfect_clock_hits_for_leads_that_put_arrival_inside_the_window():
    r = replay(0.0).report(lead_ms_now=0)
    assert r["windows"] > 100 and r["window_ms"] == 856
    g = r["hit_rate_by_lead_ms"]
    # arrival = predicted - lead + 280 ms must fall in [start, start + 856 ms)
    assert g["0"] == 1.0 and g["200"] == 1.0 and g["-200"] == 1.0
    assert g["300"] == 0.0 and g["700"] == 0.0
    assert r["hit_rate_at_configured_lead"] == 1.0 and abs(r["best_lead_ms"]) <= 200


def test_noisy_clock_lowers_hit_rate_and_reports_the_error():
    clean, noisy = replay(0.0).report(0), replay(0.6).report(0)
    assert noisy["hit_rate_at_best_lead"] < clean["hit_rate_at_best_lead"]
    assert noisy["median_abs_err_ms"] > clean["median_abs_err_ms"] and noisy["p90_abs_err_ms"] > 100
    assert "would-hit rate by lead" in format_report(noisy)


def test_no_windows_before_the_clock_is_ready():
    w = SlotWatcher("http://unused")
    d = WindowDryRun(w)
    w.listeners.append(d.on_slot)
    for i in range(40):
        w.ingest(i, 100 + i * 0.2)
    assert d.report(0) == {"windows": 0}


class FakeRpc:
    def call(self, m, p=None):
        assert m == "getClusterNodes"
        return [{"pubkey": "A", "version": "4.3.2"}, {"pubkey": "B", "version": "4.4.0"}]


def test_snapshot_flags_changes_at_measurement_time(monkeypatch):
    targets = [{"identity": "A", "client_family": "agave", "version": "4.3.2"},
               {"identity": "B", "client_family": "agave", "version": "4.3.2"}]
    snap = snapshot_targets(FakeRpc(), targets, None)
    assert [t["changed"] for t in snap] == [False, True]
    assert snap[0]["client_family"] == "agave" and snap[1]["client_family"] == "unverified"
    import itb.snapshot as sn
    monkeypatch.setattr(sn, "fetch_validators_app", lambda tok: {"B": {"software_client": "JitoLabs", "jito": True}})
    snap = snapshot_targets(FakeRpc(), targets, "tok")
    assert snap[1]["client_family"] == "jito-agave" and snap[1]["jito"] and snap[1]["changed"]
