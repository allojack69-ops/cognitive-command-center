from itertools import combinations

from flask import Blueprint, abort, render_template, session
from sqlalchemy import select

from db import SessionLocal, Participant, Run, BenchmarkPack
from stability_metrics import boundary_stability_metrics


bp = Blueprint("stability_compare", __name__)


def pct(x):
    return None if x is None else round(x * 100, 1)


@bp.get("/benchmark/<pack_id>/stability")
def compare(pack_id):
    participant_id = session.get("participant_id")

    if not participant_id:
        abort(403)

    with SessionLocal() as db:
        participant = db.get(Participant, participant_id)
        packrow = db.get(BenchmarkPack, pack_id)

        if not participant or not packrow:
            abort(404)

        pack = packrow.pack()

        if pack.get("analysis_type") != "boundary_stability":
            abort(404)

        runs = db.scalars(
            select(Run)
            .where(
                Run.participant_id == participant_id,
                Run.study_key == "BENCHMARK:" + pack_id,
                Run.status == "completed"
            )
            .order_by(
                Run.completed_at.asc(),
                Run.created_at.asc()
            )
        ).all()

        if len(runs) < 2:
            return render_template(
                "message.html",
                title="Boundary Stability",
                message=(
                    "Для міжмодельного аналізу потрібно "
                    "щонайменше два завершені runs цього probe."
                )
            )

        label_counts = {}

        for r in runs:
            name = r.model_label or "unknown"
            label_counts[name] = label_counts.get(name, 0) + 1

        seen = {}
        labels = {}
        metrics = {}
        cards = []

        for r in runs:
            name = r.model_label or "unknown"

            seen[name] = seen.get(name, 0) + 1

            display = (
                name
                if label_counts[name] == 1
                else f"{name} #{seen[name]}"
            )

            labels[r.id] = display

            m = boundary_stability_metrics(
                pack,
                r.answers
            )

            metrics[r.id] = m

            cards.append({
                "run_id": r.id,
                "model": display,
                "provider": r.provider or "—",

                "stable":
                    f"{m['strict_stable_n']}/{m['axes_n']}",

                "stability_pct":
                    pct(m["strict_stability_rate"]),

                "label_robust":
                    f"{m['label_robust_n']}/{m['axes_n']}",

                "consistency_pct":
                    pct(m["item_axis_consistency_rate"]),

                "match_n": m["match_n"],
                "abstain_n": m["abstain_n"],
                "opposite_n": m["opposite_n"],

                "conf_match":
                    m["mean_confidence_match"],

                "conf_abstain":
                    m["mean_confidence_abstain"],

                "conf_opposite":
                    m["mean_confidence_opposite"],
            })

        axis_maps = {
            r.id: {
                x["axis"]: x
                for x in metrics[r.id]["axes"]
            }
            for r in runs
        }

        axis_order = [
            x["axis"]
            for x in metrics[runs[0].id]["axes"]
        ]

        axis_rows = []
        split_axes = 0
        consensus_axes = 0

        for axis in axis_order:
            cells = []
            modal_relations = []

            reference_label = None

            for r in runs:
                x = axis_maps[r.id].get(axis)

                if not x:
                    continue

                reference_label = (
                    reference_label
                    or x.get("reference_label")
                )

                modal_relations.append(
                    x["modal_relation"]
                )

                cells.append({
                    "model": labels[r.id],
                    "modal": x["modal_relation"],
                    "stable": x["stable"],
                    "label_robust": x["label_robust"],
                    "variants": x["variants"],
                })

            consensus = (
                len(modal_relations) == len(runs)
                and len(set(modal_relations)) == 1
            )

            if consensus:
                consensus_axes += 1
                cross_class = modal_relations[0]
            else:
                split_axes += 1
                cross_class = "MODEL_SPLIT"

            axis_rows.append({
                "axis": axis,
                "reference_label": reference_label,
                "cross_class": cross_class,
                "cells": cells,
            })

        pairwise = []
        pairwise_values = []

        for left, right in combinations(runs, 2):
            same = 0
            total = 0

            lm = axis_maps[left.id]
            rm = axis_maps[right.id]

            for axis in axis_order:
                a = lm.get(axis)
                b = rm.get(axis)

                if not a or not b:
                    continue

                total += 1
                same += int(
                    a["modal_relation"]
                    == b["modal_relation"]
                )

            rate = (
                round(same / total * 100, 1)
                if total else None
            )

            if rate is not None:
                pairwise_values.append(rate)

            pairwise.append({
                "left": labels[left.id],
                "right": labels[right.id],
                "same": same,
                "total": total,
                "rate": rate,
            })

        stability_values = [
            pct(metrics[r.id]["strict_stability_rate"])
            for r in runs
            if metrics[r.id]["strict_stability_rate"] is not None
        ]

        summary = {
            "models": len(runs),
            "axes": len(axis_order),
            "consensus_axes": consensus_axes,
            "split_axes": split_axes,

            "mean_stability":
                round(
                    sum(stability_values)
                    / len(stability_values),
                    1
                )
                if stability_values
                else None,

            "mean_pairwise":
                round(
                    sum(pairwise_values)
                    / len(pairwise_values),
                    1
                )
                if pairwise_values
                else None,
        }

        participant_label = (
            participant.label
            or participant.id
        )

    return render_template(
        "compare_stability.html",
        pack=packrow,
        packdata=pack,
        participant_label=participant_label,
        cards=cards,
        axis_rows=axis_rows,
        pairwise=pairwise,
        summary=summary,
    )
