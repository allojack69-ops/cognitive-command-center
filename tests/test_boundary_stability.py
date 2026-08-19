import json
from pathlib import Path
from types import SimpleNamespace

from stability_metrics import boundary_stability_metrics


def ans(item, choice, confidence=90):
    return SimpleNamespace(
        item_id=item,
        choice=choice,
        confidence=confidence,
        actor="ai",
        phase="base",
        reason=""
    )


def load_pack():
    return json.loads(
        Path(
            "data/moral_machine_boundary_stability_v1.json"
        ).read_text(encoding="utf-8")
    )


def test_pack_structure():
    pack = load_pack()

    assert len(pack["items"]) == 27
    assert len({x["latent_axis"] for x in pack["items"]}) == 9

    assert sum(x["reference_choice"] == "A" for x in pack["items"]) == 14
    assert sum(x["reference_choice"] == "B" for x in pack["items"]) == 13


def test_perfect_relation_stability():
    pack = load_pack()

    answers = [
        ans(x["id"], x["reference_choice"])
        for x in pack["items"]
    ]

    m = boundary_stability_metrics(pack, answers)

    assert m["axes_n"] == 9
    assert m["strict_stable_n"] == 9
    assert m["label_robust_n"] == 9
    assert m["strict_stability_rate"] == 1.0
    assert m["item_axis_consistency_rate"] == 1.0
    assert m["match_n"] == 27


def test_one_variant_breaks_one_axis():
    pack = load_pack()

    answers = []

    for i, x in enumerate(pack["items"]):
        choice = x["reference_choice"]

        if i == 0:
            choice = "C"

        answers.append(ans(x["id"], choice))

    m = boundary_stability_metrics(pack, answers)

    assert m["strict_stable_n"] == 8
    assert m["label_robust_n"] == 8
    assert m["abstain_n"] == 1
