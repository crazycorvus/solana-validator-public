from itb.figures import make_figures
from itb.infer import verdict_for
from test_infer import make


def build(tmp_path, sub):
    bs = {"AAAAAAAAfee": make("fee", 1), "BBBBBBBBrnd": make("random", 2)}
    ver = [verdict_for(b, 1, 300, label=k) for k, b in bs.items()]
    return make_figures(bs, ver, tmp_path / sub)


def test_figures_written_and_deterministic(tmp_path):
    a, b = build(tmp_path, "a"), build(tmp_path, "b")
    assert len(a) == 3 and all(p.stat().st_size > 5_000 for p in a)
    assert [p.read_bytes() for p in a] == [p.read_bytes() for p in b]
