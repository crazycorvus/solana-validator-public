from itb.schedule import LeaderSchedule, SlotClock
from itb.select import select_targets


def sched():
    m = {}
    for base in range(1000, 1000 + 4 * 40, 4):
        who = "A" if ((base - 1000) // 4) % 3 == 0 else "B" if ((base - 1000) // 4) % 3 == 1 else "C"
        for s in range(base, base + 4):
            m[s] = who
    m[1005] = "A"  # contaminates B window at 1004
    return LeaderSchedule(1, 1000, m)


def test_windows_are_full_aligned_and_after_slot():
    s = sched()
    assert s.windows("A", 999, 1) == [[1000, 1001, 1002, 1003]]
    assert all(w[0] % 4 == 0 for w in s.windows("C", 999))
    assert [1004, 1005, 1006, 1007] not in s.windows("B", 999)  # contaminated by A
    assert s.windows("A", 1000, 1)[0][0] > 1000


def test_slot_clock_predicts_linear():
    c = SlotClock()
    for i in range(50):
        c.add(100 + i, 10 + i * 0.4)
    assert abs(c.predict(200) - (10 + 100 * 0.4)) < 1e-6


def test_selector_diversity_and_min_windows():
    s = sched()
    rows = [{"identity": i, "vote_pubkey": i, "stake": st, "version": "x", "client_family": f}
            for i, st, f in [("A", 9, "agave"), ("B", 8, "agave"), ("C", 1, "fd"), ("Z", 99, "agave")]]
    chosen, log = select_targets(rows, s, 999, n=3, min_families=2, min_windows=3)
    ids = {c["identity"] for c in chosen}
    assert "Z" not in ids  # no windows
    assert {"A", "C"} <= ids


def test_slot_clock_recovers_rate_and_predicts_under_jitter():
    import random
    rng = random.Random(1)
    c = SlotClock()
    for i in range(300):  # 0.214 s/slot with +-120 ms notification jitter
        c.add(1000 + i, 50 + i * 0.214 + rng.uniform(-0.12, 0.12))
    assert abs(c.rate() - 0.214) < 0.002
    assert abs(c.predict(1000 + 300 + 117) - (50 + 417 * 0.214)) < 0.2  # ~25 s ahead: endpoint-span rate is noisy, see predict() docstring
