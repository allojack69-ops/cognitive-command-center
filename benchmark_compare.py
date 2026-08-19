from itertools import combinations

from flask import Blueprint, abort, render_template, session
from sqlalchemy import select

from db import SessionLocal, Participant, Run, BenchmarkPack
from metrics import benchmark_metrics


bp = Blueprint("benchmark_compare", __name__)


def pct(v):
    return None if v is None else round(v * 100, 1)


@bp.get("/benchmark/<pack_id>/compare")
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
                title="Benchmark comparison",
                message=(
                    "Для порівняння потрібно щонайменше "
                    "два завершені runs цього benchmark."
                )
            )

        # ----------------------------------------------------
        # Display labels
        # ----------------------------------------------------
        label_counts = {}

        for r in runs:
            key = r.model_label or "unknown"
            label_counts[key] = label_counts.get(key, 0) + 1

        seen = {}
        display_labels = {}

        answer_maps = {}
        metric_maps = {}
        relation_maps = {}

        model_rows = []

        for r in runs:
            model = r.model_label or "unknown"

            seen[model] = seen.get(model, 0) + 1

            if label_counts[model] == 1:
                display = model
            else:
                display = f"{model} #{seen[model]}"

            display_labels[r.id] = display

            answers = {
                a.item_id: a
                for a in r.answers
                if a.actor == "ai"
                and a.phase == "base"
            }

            metrics = benchmark_metrics(
                pack,
                r.answers
            )

            answer_maps[r.id] = answers
            metric_maps[r.id] = metrics

            relation_maps[r.id] = {
                x["item_id"]: x
                for x in metrics.get("rows", [])
            }

            model_rows.append({
                "run_id": r.id,
                "model": display,
                "provider": r.provider or "—",
                "account": r.account_alias or "—",
                "personalization": r.personalization or "—",

                "match":
                    f"{metrics.get('reference_match_n', 0)}/"
                    f"{metrics.get('reference_items_n', 0)}",

                "abstain":
                    f"{metrics.get('reference_abstain_n', 0)}/"
                    f"{metrics.get('reference_items_n', 0)}",

                "opposite":
                    f"{metrics.get('reference_opposite_n', 0)}/"
                    f"{metrics.get('reference_items_n', 0)}",

                "conditional":
                    pct(metrics.get(
                        "conditional_alignment_rate"
                    )),

                "conf_match":
                    metrics.get(
                        "mean_confidence_match"
                    ),

                "conf_abstain":
                    metrics.get(
                        "mean_confidence_abstain"
                    ),

                "conf_opposite":
                    metrics.get(
                        "mean_confidence_opposite"
                    )
            })

        # ----------------------------------------------------
        # Item matrix
        # ----------------------------------------------------
        item_rows = []

        consensus_match = 0
        consensus_abstain = 0
        consensus_opposite = 0
        boundary_split = 0

        for item in pack.get("items", []):
            iid = item["id"]

            cells = []
            relations = []

            for r in runs:
                a = answer_maps[r.id].get(iid)
                analytical = (
                    relation_maps[r.id].get(iid, {})
                )

                relation = (
                    analytical.get(
                        "reference_relation"
                    )
                    or "—"
                )

                if relation != "—":
                    relations.append(relation)

                cells.append({
                    "choice":
                        a.choice
                        if a else "—",

                    "confidence":
                        int(a.confidence)
                        if (
                            a
                            and a.confidence
                            is not None
                        )
                        else None,

                    "relation":
                        relation,

                    "reason":
                        a.reason
                        if (
                            a
                            and a.reason
                        )
                        else ""
                })

            unanimous = (
                len(relations) == len(runs)
                and len(set(relations)) == 1
            )

            if unanimous:
                row_class = relations[0]
            else:
                row_class = "SPLIT"

            if row_class == "MATCH":
                consensus_match += 1

            elif row_class == "ABSTAIN":
                consensus_abstain += 1

            elif row_class == "OPPOSITE":
                consensus_opposite += 1

            else:
                boundary_split += 1

            item_rows.append({
                "id":
                    iid,

                "dimension":
                    item.get(
                        "dimension",
                        ""
                    ),

                "reference_choice":
                    item.get(
                        "reference_choice",
                        "—"
                    ),

                "reference_label":
                    item.get(
                        "reference_label",
                        ""
                    ),

                "class":
                    row_class,

                "cells":
                    cells
            })

        consensus_rows = [
            x
            for x in item_rows
            if x["class"] != "SPLIT"
        ]

        split_rows = [
            x
            for x in item_rows
            if x["class"] == "SPLIT"
        ]

        # ----------------------------------------------------
        # Pairwise choice agreement
        # ----------------------------------------------------
        pairwise = []
        pairwise_rates = []

        for left, right in combinations(
            runs,
            2
        ):
            same = 0
            total = 0

            for item in pack.get(
                "items",
                []
            ):
                iid = item["id"]

                a = answer_maps[
                    left.id
                ].get(iid)

                b = answer_maps[
                    right.id
                ].get(iid)

                if a and b:
                    total += 1
                    same += int(
                        a.choice == b.choice
                    )

            rate = (
                round(
                    same / total * 100,
                    1
                )
                if total
                else None
            )

            if rate is not None:
                pairwise_rates.append(
                    rate
                )

            pairwise.append({
                "left":
                    display_labels[left.id],

                "right":
                    display_labels[right.id],

                "same":
                    same,

                "total":
                    total,

                "rate":
                    rate
            })

        mean_pairwise = (
            round(
                sum(pairwise_rates)
                / len(pairwise_rates),
                1
            )
            if pairwise_rates
            else None
        )

        summary = {
            "models":
                len(runs),

            "items":
                len(
                    pack.get(
                        "items",
                        []
                    )
                ),

            "consensus_match":
                consensus_match,

            "consensus_abstain":
                consensus_abstain,

            "consensus_opposite":
                consensus_opposite,

            "boundary_split":
                boundary_split,

            "mean_pairwise":
                mean_pairwise
        }

        participant_label = (
            participant.label
            or participant.id
        )

    return render_template(
        "compare_benchmark.html",

        pack=packrow,
        packdata=pack,

        participant_label=
            participant_label,

        model_rows=
            model_rows,

        item_rows=
            item_rows,

        consensus_rows=
            consensus_rows,

        split_rows=
            split_rows,

        pairwise=
            pairwise,

        summary=
            summary
    )
