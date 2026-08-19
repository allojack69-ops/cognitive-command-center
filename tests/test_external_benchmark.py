from types import SimpleNamespace
import json
from pathlib import Path

from metrics import benchmark_metrics


def ans(item_id, choice, confidence=90):
    return SimpleNamespace(
        item_id=item_id,
        choice=choice,
        confidence=confidence,
        actor="ai",
        phase="base"
    )


def test_moral_machine_directional_pack():
    pack=json.loads(
        Path("data/moral_machine_global_direction_v1.json")
        .read_text(encoding="utf-8")
    )

    answers=[
        ans(item["id"],item["reference_choice"])
        for item in pack["items"]
    ]

    m=benchmark_metrics(pack,answers)

    assert m["items_n"] == 9
    assert m["reference_items_n"] == 9
    assert m["reference_agreement_n"] == 9
    assert m["reference_agreement_rate"] == 1.0
    assert m["distribution_items_n"] == 0


def test_directional_mismatch_and_abstention():
    pack={
        "items":[
            {
                "id":"X1",
                "reference_choice":"A",
                "options":{"A":"a","B":"b","C":"c"}
            },
            {
                "id":"X2",
                "reference_choice":"B",
                "options":{"A":"a","B":"b","C":"c"}
            }
        ]
    }

    answers=[
        ans("X1","B"),
        ans("X2","C")
    ]

    m=benchmark_metrics(pack,answers)

    assert m["reference_agreement_rate"] == 0.0
    assert m["abstention_rate"] == 0.5
