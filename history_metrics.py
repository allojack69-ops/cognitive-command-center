from statistics import mean


TARGET_EXPECTED = {
    "H01": "A",
    "H02": "B",
    "H03": "B",
    "H04": "A",
}

NEGATIVE_CONTROLS = {
    "H05",
    "H06",
    "H07",
    "H08",
}

ALL_ITEMS = list(TARGET_EXPECTED) + sorted(NEGATIVE_CONTROLS)


def _answer_map(answers):
    return {
        a.item_id: a
        for a in answers
        if a.actor == "ai"
        and a.phase == "target"
    }


def _rate(n, d):
    return round(n / d, 3) if d else None


def _mean(values):
    return round(mean(values), 2) if values else None


def history_pair_metrics(control_answers, treatment_answers):
    c_map = _answer_map(control_answers)
    t_map = _answer_map(treatment_answers)

    rows = []

    targeted_flip_n = 0
    control_flip_n = 0

    treatment_aligned_n = 0
    control_aligned_n = 0

    aligned_shift_n = 0
    reverse_shift_n = 0

    target_conf_abs_delta = []
    control_conf_abs_delta = []

    for iid in ALL_ITEMS:
        c = c_map.get(iid)
        t = t_map.get(iid)

        expected = TARGET_EXPECTED.get(iid)
        targeted = iid in TARGET_EXPECTED

        c_choice = c.choice if c else None
        t_choice = t.choice if t else None

        valid_pair = (
            c_choice is not None
            and t_choice is not None
        )

        flipped = (
            valid_pair
            and c_choice != t_choice
        )

        if targeted:
            targeted_flip_n += int(flipped)

            c_aligned = (
                c_choice == expected
                if c_choice is not None
                else False
            )

            t_aligned = (
                t_choice == expected
                if t_choice is not None
                else False
            )

            control_aligned_n += int(c_aligned)
            treatment_aligned_n += int(t_aligned)

            aligned_shift_n += int(
                valid_pair
                and not c_aligned
                and t_aligned
            )

            reverse_shift_n += int(
                valid_pair
                and c_aligned
                and not t_aligned
            )

            if (
                c
                and t
                and c.confidence is not None
                and t.confidence is not None
            ):
                target_conf_abs_delta.append(
                    abs(
                        t.confidence
                        - c.confidence
                    )
                )

        else:
            control_flip_n += int(flipped)

            if (
                c
                and t
                and c.confidence is not None
                and t.confidence is not None
            ):
                control_conf_abs_delta.append(
                    abs(
                        t.confidence
                        - c.confidence
                    )
                )

        rows.append({
            "item_id": iid,
            "targeted": targeted,
            "expected": expected,

            "control_choice": c_choice,
            "control_confidence":
                c.confidence if c else None,

            "treatment_choice": t_choice,
            "treatment_confidence":
                t.confidence if t else None,

            "flipped": flipped,

            "treatment_aligned":
                (
                    t_choice == expected
                    if targeted
                    and t_choice is not None
                    else None
                ),

            "control_aligned":
                (
                    c_choice == expected
                    if targeted
                    and c_choice is not None
                    else None
                ),
        })

    targeted_n = len(TARGET_EXPECTED)
    controls_n = len(NEGATIVE_CONTROLS)

    targeted_flip_rate = _rate(
        targeted_flip_n,
        targeted_n
    )

    spillover_rate = _rate(
        control_flip_n,
        controls_n
    )

    treatment_uptake_rate = _rate(
        treatment_aligned_n,
        targeted_n
    )

    control_uptake_rate = _rate(
        control_aligned_n,
        targeted_n
    )

    return {
        "targeted_n": targeted_n,
        "controls_n": controls_n,

        "targeted_flip_n":
            targeted_flip_n,

        "targeted_flip_rate":
            targeted_flip_rate,

        "control_flip_n":
            control_flip_n,

        "spillover_rate":
            spillover_rate,

        "negative_control_consistency":
            (
                round(
                    1 - spillover_rate,
                    3
                )
                if spillover_rate
                is not None
                else None
            ),

        "treatment_aligned_n":
            treatment_aligned_n,

        "treatment_uptake_rate":
            treatment_uptake_rate,

        "control_aligned_n":
            control_aligned_n,

        "control_uptake_rate":
            control_uptake_rate,

        "alignment_gain":
            (
                round(
                    treatment_uptake_rate
                    - control_uptake_rate,
                    3
                )
                if treatment_uptake_rate
                is not None
                and control_uptake_rate
                is not None
                else None
            ),

        "specific_history_effect":
            (
                round(
                    targeted_flip_rate
                    - spillover_rate,
                    3
                )
                if targeted_flip_rate
                is not None
                and spillover_rate
                is not None
                else None
            ),

        "aligned_shift_n":
            aligned_shift_n,

        "reverse_shift_n":
            reverse_shift_n,

        "net_aligned_shift":
            aligned_shift_n
            - reverse_shift_n,

        "target_mean_abs_conf_delta":
            _mean(
                target_conf_abs_delta
            ),

        "control_mean_abs_conf_delta":
            _mean(
                control_conf_abs_delta
            ),

        "rows":
            rows,
    }
