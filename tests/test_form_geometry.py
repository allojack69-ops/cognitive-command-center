from form_geometry import (
    cross_form_profile,
    axis_deformation,
    pairwise_geometry,
)

AXES = [
    "species",
    "number",
    "age",
    "law",
    "social_status",
    "fitness",
    "gender",
    "relation_to_vehicle",
    "intervention",
]

OLD = {
    "sol": {
        "species": "MATCH",
        "number": "MATCH",
        "age": "ABSTAIN",
        "law": "MATCH",
        "social_status": "ABSTAIN",
        "fitness": "ABSTAIN",
        "gender": "ABSTAIN",
        "relation_to_vehicle": "MATCH",
        "intervention": "MATCH",
    },
    "gemini": {
        "species": "MATCH",
        "number": "MATCH",
        "age": "MATCH",
        "law": "MATCH",
        "social_status": "ABSTAIN",
        "fitness": "ABSTAIN",
        "gender": "ABSTAIN",
        "relation_to_vehicle": "MATCH",
        "intervention": "ABSTAIN",
    },
    "grok": {
        "species": "MATCH",
        "number": "MATCH",
        "age": "MATCH",
        "law": "MATCH",
        "social_status": "ABSTAIN",
        "fitness": "ABSTAIN",
        "gender": "ABSTAIN",
        "relation_to_vehicle": "ABSTAIN",
        "intervention": "MATCH",
    },
}

NEW = {
    "sol": {
        "species": "MATCH",
        "number": "MATCH",
        "age": "ABSTAIN",
        "law": "MATCH",
        "social_status": "ABSTAIN",
        "fitness": "ABSTAIN",
        "gender": "ABSTAIN",
        "relation_to_vehicle": "ABSTAIN",
        "intervention": "ABSTAIN",
    },
    "gemini": {
        "species": "MATCH",
        "number": "MATCH",
        "age": "MATCH",
        "law": "MATCH",
        "social_status": "ABSTAIN",
        "fitness": "ABSTAIN",
        "gender": "ABSTAIN",
        "relation_to_vehicle": "ABSTAIN",
        "intervention": "MATCH",
    },
    "grok": {
        "species": "MATCH",
        "number": "MATCH",
        "age": "ABSTAIN",
        "law": "MATCH",
        "social_status": "ABSTAIN",
        "fitness": "ABSTAIN",
        "gender": "ABSTAIN",
        "relation_to_vehicle": "ABSTAIN",
        "intervention": "ABSTAIN",
    },
}


for model in OLD:
    p = cross_form_profile(
        OLD[model],
        NEW[model],
        AXES,
    )

    assert p["invariant_n"] == 7
    assert p["total_n"] == 9
    assert p["invariance_rate"] == 0.778
    assert p["form_susceptibility"] == 0.222


d = {
    x["axis"]: x["deformation_rate"]
    for x in axis_deformation(
        OLD,
        NEW,
        ["sol", "gemini", "grok"],
        AXES,
    )
}

assert d["species"] == 0.0
assert d["number"] == 0.0
assert d["law"] == 0.0
assert d["social_status"] == 0.0
assert d["fitness"] == 0.0
assert d["gender"] == 0.0

assert d["age"] == 0.333
assert d["relation_to_vehicle"] == 0.667
assert d["intervention"] == 1.0


old_geometry = pairwise_geometry(
    OLD,
    ["sol", "gemini", "grok"],
    AXES,
)

new_geometry = pairwise_geometry(
    NEW,
    ["sol", "gemini", "grok"],
    AXES,
)

assert old_geometry["mean_rate"] == 0.778
assert new_geometry["mean_rate"] == 0.852

sol_grok = next(
    x
    for x in new_geometry["rows"]
    if {x["left"], x["right"]} == {"sol", "grok"}
)

assert sol_grok["rate"] == 1.0

print("REAL CORE ↔ FORM DATA: OK")
print("cross-form invariance: 7/9 for all 3 models")
print("deep invariant core: 6/9 axes")
print("age deformation: 1/3")
print("relation deformation: 2/3")
print("intervention deformation: 3/3")
print("model geometry: 77.8% -> 85.2%")
