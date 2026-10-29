import json
import shutil

from test_pipeline import run_pipeline
from itb.env import rpc_host, write_env
from itb.infer import analyze, load_batches
from itb.noise import noise_report
from itb.report import build_report


def data_dir(tmp_path, run_name="20261029T100000Z"):
    b, rows, res, led = run_pipeline(tmp_path / "w", "fee")
    d = tmp_path / "data"
    run = d / "raw" / run_name
    shutil.copytree(tmp_path / "w" / "run", run)
    write_env(run, "https://solana-mainnet.g.alchemy.com/v2/SECRETKEY123",
              [{"identity": "LEADER", "client_family": "agave", "version": "4.3.2"}])
    runs = [run]
    analyze(runs, d / "analysis/verdicts.json", 1, 200)
    (d / "analysis/noise.json").write_text(json.dumps(noise_report(load_batches(runs), runs, 1, 100)))
    return d, rows


def test_report_contains_claims_and_evidence(tmp_path):
    d, rows = data_dir(tmp_path)
    md = build_report(d, tmp_path / "report")
    assert "## Summary" in md and "## Measurement log" in md and "## What went wrong" in md
    assert "agave 4.3.2" in md and "2026-10-29" in md
    for r in rows:  # every probe's signature is linked with its slot
        assert r["signature"][:12] in md and f"https://solscan.io/tx/{r['signature']}" in md
    assert "insufficient data" in md  # one 8-probe batch is below the 16-probe floor
    assert "SECRETKEY123" not in md and "alchemy" in md


def test_report_is_deterministic_and_excludes_pre_alpenglow(tmp_path):
    d, _ = data_dir(tmp_path, run_name="20261001T100000Z")
    a = build_report(d, tmp_path / "report")
    assert a == build_report(d, tmp_path / "report")
    assert "No post-Alpenglow measurement data" in a
    assert "dated before Alpenglow" in a and "20261001T100000Z" in a


def test_env_never_stores_the_key(tmp_path):
    env = write_env(tmp_path, "https://x.example/v2/SECRETKEY123?api-key=SECRETKEY123")
    assert "SECRETKEY123" not in (tmp_path / "env.json").read_text()
    assert rpc_host("https://x.example/v2/K") == "x.example" and env["rpc_provider_host"] == "x.example"


def test_report_flags_client_change_between_selection_and_measurement(tmp_path):
    d, _ = data_dir(tmp_path)
    run = next((d / "raw").glob("2*"))
    write_env(run, "https://x.example/v2/K", [{"identity": "LEADER", "client_family": "agave", "version": "4.3.2"}],
              snapshot=[{"identity": "LEADER", "client_family": "jito-agave", "version": "4.3.3", "changed": True}])
    md = build_report(d, tmp_path / "report")
    assert "jito-agave 4.3.3" in md and "changed client/version between target selection and measurement" in md
