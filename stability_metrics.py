from collections import Counter, defaultdict
from statistics import mean


def _mean(values):
    return round(mean(values), 2) if values else None


def boundary_stability_metrics(pack, answers):
    abstention = pack.get("abstention_choice", "C")

    amap = {
        a.item_id: a
        for a in answers
        if a.actor == "ai" and a.phase == "base"
    }

    grouped = defaultdict(list)
    axis_order = []

    relation_confidence = {
        "MATCH": [],
        "ABSTAIN": [],
        "OPPOSITE": [],
    }

    total_relations = Counter()
    answered_items = 0

    for item in pack.get("items", []):
        axis = item.get("latent_axis") or item.get("dimension") or item["id"]

        if axis not in grouped:
            axis_order.append(axis)

        a = amap.get(item["id"])

        if not a:
            continue

        answered_items += 1

        if a.choice == item.get("reference_choice"):
            relation = "MATCH"

        elif a.choice == abstention:
            relation = "ABSTAIN"

        else:
            relation = "OPPOSITE"

        total_relations[relation] += 1

        if a.confidence is not None:
            relation_confidence[relation].append(a.confidence)

        grouped[axis].append({
            "item_id": item["id"],
            "variant": item.get("variant"),
            "surface_form": item.get("surface_form"),
            "reference_choice": item.get("reference_choice"),
            "reference_label": item.get("reference_label"),
            "choice": a.choice,
            "confidence": a.confidence,
            "relation": relation,
            "reason": a.reason or "",
        })

    axes = []

    strict_stable_n = 0
    label_robust_n = 0
    modal_items_n = 0

    for axis in axis_order:
        variants = sorted(
            grouped.get(axis, []),
            key=lambda x: (
                x["variant"] is None,
                x["variant"] if x["variant"] is not None else 999
            )
        )

        if not variants:
            continue

        rels = [x["relation"] for x in variants]
        counts = Counter(rels)

        modal_relation, modal_count = counts.most_common(1)[0]

        stable = len(counts) == 1

        reference_sides = {
            x["reference_choice"]
            for x in variants
            if x["reference_choice"] in ("A", "B")
        }

        has_label_flip = reference_sides == {"A", "B"}
        label_robust = stable and has_label_flip

        strict_stable_n += int(stable)
        label_robust_n += int(label_robust)
        modal_items_n += modal_count

        axes.append({
            "axis": axis,
            "reference_label": variants[0].get("reference_label"),
            "variants_n": len(variants),
            "modal_relation": modal_relation,
            "modal_count": modal_count,
            "stable": stable,
            "has_label_flip": has_label_flip,
            "label_robust": label_robust,
            "relations": rels,
            "variants": variants,
        })

    axes_n = len(axes)

    return {
        "items_n": answered_items,
        "axes_n": axes_n,

        "strict_stable_n": strict_stable_n,
        "strict_stability_rate":
            round(strict_stable_n / axes_n, 3)
            if axes_n else None,

        "label_robust_n": label_robust_n,
        "label_robust_rate":
            round(label_robust_n / axes_n, 3)
            if axes_n else None,

        "item_axis_consistency_rate":
            round(modal_items_n / answered_items, 3)
            if answered_items else None,

        "match_n": total_relations["MATCH"],
        "abstain_n": total_relations["ABSTAIN"],
        "opposite_n": total_relations["OPPOSITE"],

        "mean_confidence_match":
            _mean(relation_confidence["MATCH"]),

        "mean_confidence_abstain":
            _mean(relation_confidence["ABSTAIN"]),

        "mean_confidence_opposite":
            _mean(relation_confidence["OPPOSITE"]),

        "axes": axes,
    }
