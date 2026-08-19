from types import SimpleNamespace

from history_metrics import (
    history_pair_metrics
)


def ans(iid, choice, confidence=90):
    return SimpleNamespace(
        item_id=iid,
        choice=choice,
        confidence=confidence,
        actor="ai",
        phase="target",
    )


control = [
    ans("H01", "C"),
    ans("H02", "C"),
    ans("H03", "C"),
    ans("H04", "C"),

    ans("H05", "C"),
    ans("H06", "C"),
    ans("H07", "C"),
    ans("H08", "C"),
]

treatment = [
    ans("H01", "A"),
    ans("H02", "B"),
    ans("H03", "B"),
    ans("H04", "A"),

    ans("H05", "C"),
    ans("H06", "C"),
    ans("H07", "C"),
    ans("H08", "C"),
]

m = history_pair_metrics(
    control,
    treatment
)

assert m["targeted_flip_n"] == 4
assert m["targeted_flip_rate"] == 1.0

assert m["control_flip_n"] == 0
assert m["spillover_rate"] == 0.0

assert m["treatment_uptake_rate"] == 1.0
assert m["control_uptake_rate"] == 0.0

assert m["alignment_gain"] == 1.0
assert m["specific_history_effect"] == 1.0

assert m["aligned_shift_n"] == 4
assert m["reverse_shift_n"] == 0
assert m["net_aligned_shift"] == 4

print("HISTORY METRICS: OK")
