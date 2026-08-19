from flask import Blueprint, abort, render_template, session
from sqlalchemy import select

from db import SessionLocal, Participant, Run, BenchmarkPack
from metrics import benchmark_metrics
from stability_metrics import boundary_stability_metrics
from form_geometry import (
    cross_form_profile,
    axis_deformation,
    pairwise_geometry,
)

bp = Blueprint("form_dashboard", __name__)

OLD_PACK = "MORAL-MACHINE-GLOBAL-DIRECTION-V1"
NEW_PACK = "MORAL-MACHINE-BOUNDARY-STABILITY-V1"


def canon(label):
    return " ".join(
        (label or "unknown")
        .strip()
        .lower()
        .split()
    )


def latest_by_model(runs):
    out = {}

    for run in runs:
        out[canon(run.model_label)] = run

    return out


def pct(x):
    return None if x is None else round(x * 100, 1)


@bp.get("/benchmark/presentation-geometry")
def presentation_geometry():
    participant_id = session.get("participant_id")

    if not participant_id:
        abort(403)

    with SessionLocal() as db:
        participant = db.get(
            Participant,
            participant_id
        )

        old_pack_row = db.get(
            BenchmarkPack,
            OLD_PACK
        )

        new_pack_row = db.get(
            BenchmarkPack,
            NEW_PACK
        )

        if (
            not participant
            or not old_pack_row
            or not new_pack_row
        ):
            abort(404)

        old_pack = old_pack_row.pack()
        new_pack = new_pack_row.pack()

        old_runs = db.scalars(
            select(Run)
            .where(
                Run.participant_id == participant_id,
                Run.study_key == "BENCHMARK:" + OLD_PACK,
                Run.status == "completed",
            )
            .order_by(
                Run.completed_at.asc(),
                Run.created_at.asc(),
            )
        ).all()

        new_runs = db.scalars(
            select(Run)
            .where(
                Run.participant_id == participant_id,
                Run.study_key == "BENCHMARK:" + NEW_PACK,
                Run.status == "completed",
            )
            .order_by(
                Run.completed_at.asc(),
                Run.created_at.asc(),
            )
        ).all()

        old_latest = latest_by_model(old_runs)
        new_latest = latest_by_model(new_runs)

        models = sorted(
            set(old_latest)
            & set(new_latest)
        )

        if not models:
            abort(404)

        axes = []
        axis_labels = {}

        for item in new_pack.get("items", []):
            axis = item.get("latent_axis")

            if not axis:
                continue

            if axis not in axes:
                axes.append(axis)

            axis_labels[axis] = (
                item.get("reference_label")
                or axis
            )

        old_maps = {}
        new_maps = {}
        strict_maps = {}
        labels = {}

        for model in models:
            old_run = old_latest[model]
            new_run = new_latest[model]

            labels[model] = (
                new_run.model_label
                or old_run.model_label
                or model
            )

            old_m = benchmark_metrics(
                old_pack,
                old_run.answers,
            )

            old_maps[model] = {
                row["dimension"]:
                    row["reference_relation"]

                for row in old_m["rows"]

                if row.get("dimension")
                and row.get("reference_relation")
            }

            new_m = boundary_stability_metrics(
                new_pack,
                new_run.answers,
            )

            new_maps[model] = {
                row["axis"]:
                    row["modal_relation"]

                for row in new_m["axes"]
            }

            strict_maps[model] = {
                row["axis"]:
                    row["stable"]

                for row in new_m["axes"]
            }

        # --------------------------------------
        # Model-level invariance
        # --------------------------------------

        model_cards = []

        for model in models:
            profile = cross_form_profile(
                old_maps[model],
                new_maps[model],
                axes,
            )

            shifts = [
                row
                for row in profile["rows"]
                if (
                    row["old"] is not None
                    and row["new"] is not None
                    and not row["invariant"]
                )
            ]

            model_cards.append({
                "model":
                    labels[model],

                "invariant_n":
                    profile["invariant_n"],

                "total_n":
                    profile["total_n"],

                "invariance_pct":
                    pct(
                        profile[
                            "invariance_rate"
                        ]
                    ),

                "susceptibility_pct":
                    pct(
                        profile[
                            "form_susceptibility"
                        ]
                    ),

                "shifts":
                    shifts,
            })

        # --------------------------------------
        # Axis deformation
        # --------------------------------------

        deformation = axis_deformation(
            old_maps,
            new_maps,
            models,
            axes,
        )

        axis_rows = []
        deep_core_n = 0

        for d in deformation:
            axis = d["axis"]

            cells = []
            old_values = []
            new_values = []

            for model in models:
                old = old_maps[model].get(axis)
                new = new_maps[model].get(axis)

                old_values.append(old)
                new_values.append(new)

                cells.append({
                    "model":
                        labels[model],

                    "old":
                        old,

                    "new":
                        new,

                    "invariant":
                        old == new,

                    "new_form_strict":
                        strict_maps[
                            model
                        ].get(
                            axis,
                            False
                        ),
                })

            valid = (
                None not in old_values
                and None not in new_values
            )

            deep_core = (
                valid
                and all(
                    a == b
                    for a, b
                    in zip(
                        old_values,
                        new_values
                    )
                )
                and len(
                    set(old_values)
                ) == 1
                and len(
                    set(new_values)
                ) == 1
            )

            if deep_core:
                deep_core_n += 1

            axis_rows.append({
                "axis":
                    axis,

                "label":
                    axis_labels.get(
                        axis,
                        axis
                    ),

                "deep_core":
                    deep_core,

                "core_relation":
                    (
                        old_values[0]
                        if deep_core
                        else None
                    ),

                "shifted_models":
                    d["shifted"],

                "models":
                    d["models"],

                "deformation_pct":
                    pct(
                        d[
                            "deformation_rate"
                        ]
                    ),

                "cells":
                    cells,
            })

        axis_rows.sort(
            key=lambda x: (
                x["deep_core"],
                -(
                    x[
                        "deformation_pct"
                    ]
                    or 0
                ),
                x["axis"],
            )
        )

        # --------------------------------------
        # Geometry deformation
        # --------------------------------------

        old_geometry = pairwise_geometry(
            old_maps,
            models,
            axes,
        )

        new_geometry = pairwise_geometry(
            new_maps,
            models,
            axes,
        )

        new_lookup = {
            (
                x["left"],
                x["right"]
            ): x

            for x
            in new_geometry["rows"]
        }

        geometry_rows = []

        for old in old_geometry["rows"]:
            key = (
                old["left"],
                old["right"]
            )

            new = new_lookup.get(key)

            if not new:
                continue

            geometry_rows.append({
                "left":
                    labels[
                        old["left"]
                    ],

                "right":
                    labels[
                        old["right"]
                    ],

                "old_pct":
                    pct(
                        old["rate"]
                    ),

                "new_pct":
                    pct(
                        new["rate"]
                    ),

                "delta_pp":
                    round(
                        (
                            new["rate"]
                            - old["rate"]
                        )
                        * 100,
                        1
                    ),
            })

        invariance_values = [
            x["invariance_pct"]
            for x in model_cards
            if x["invariance_pct"]
            is not None
        ]

        summary = {
            "models":
                len(models),

            "axes":
                len(axes),

            "deep_core_axes":
                deep_core_n,

            "presentation_sensitive_axes":
                len(axes)
                - deep_core_n,

            "mean_cross_presentation_invariance":
                (
                    round(
                        sum(
                            invariance_values
                        )
                        / len(
                            invariance_values
                        ),
                        1
                    )
                    if invariance_values
                    else None
                ),

            "old_pairwise_pct":
                pct(
                    old_geometry[
                        "mean_rate"
                    ]
                ),

            "new_pairwise_pct":
                pct(
                    new_geometry[
                        "mean_rate"
                    ]
                ),
        }

        participant_label = (
            participant.label
            or participant.id
        )

    return render_template(
        "compare_presentation_geometry.html",

        participant_label=
            participant_label,

        model_cards=
            model_cards,

        axis_rows=
            axis_rows,

        geometry_rows=
            geometry_rows,

        summary=
            summary,
    )
