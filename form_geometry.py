from itertools import combinations


def agreement(left, right, axes):
    same = 0
    total = 0

    for axis in axes:
        a = left.get(axis)
        b = right.get(axis)

        if a is None or b is None:
            continue

        total += 1
        same += int(a == b)

    return {
        "same": same,
        "total": total,
        "rate": round(same / total, 3) if total else None,
    }


def cross_form_profile(old_map, new_map, axes):
    rows = []
    invariant = 0
    total = 0

    for axis in axes:
        old = old_map.get(axis)
        new = new_map.get(axis)

        valid = old is not None and new is not None

        if valid:
            total += 1

        same = valid and old == new
        invariant += int(same)

        rows.append({
            "axis": axis,
            "old": old,
            "new": new,
            "invariant": same,
            "transition": (
                f"{old} → {new}"
                if valid
                else "—"
            ),
        })

    return {
        "rows": rows,
        "invariant_n": invariant,
        "total_n": total,
        "invariance_rate": (
            round(invariant / total, 3)
            if total else None
        ),
        "form_susceptibility": (
            round(1 - invariant / total, 3)
            if total else None
        ),
    }


def axis_deformation(old_by_model, new_by_model, models, axes):
    rows = []

    for axis in axes:
        shifted = 0
        valid = 0

        for model in models:
            old = old_by_model.get(model, {}).get(axis)
            new = new_by_model.get(model, {}).get(axis)

            if old is None or new is None:
                continue

            valid += 1
            shifted += int(old != new)

        rows.append({
            "axis": axis,
            "shifted": shifted,
            "models": valid,
            "deformation_rate": (
                round(shifted / valid, 3)
                if valid else None
            ),
        })

    return rows


def pairwise_geometry(maps, models, axes):
    rows = []

    for left, right in combinations(models, 2):
        result = agreement(
            maps[left],
            maps[right],
            axes,
        )

        rows.append({
            "left": left,
            "right": right,
            **result,
        })

    rates = [
        x["rate"]
        for x in rows
        if x["rate"] is not None
    ]

    return {
        "rows": rows,
        "mean_rate": (
            round(sum(rates) / len(rates), 3)
            if rates else None
        ),
    }
