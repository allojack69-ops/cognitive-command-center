import json
import random
import secrets

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from sqlalchemy import select

from db import (
    SessionLocal,
    Participant,
    Run,
    Answer,
    log_event,
    utcnow,
)

from history_metrics import (
    ALL_ITEMS,
    history_pair_metrics,
)


bp = Blueprint(
    "history_probe",
    __name__
)


STUDY_KEY = "HISTORY_INTERVENTION_V01"


TARGET_ITEMS = {
    "H01": {
        "text":
            "Система Kappa має вибрати канал "
            "для наступного циклу. Обидва канали "
            "зараз пройшли однакову діагностику; "
            "нових спостережень немає.",

        "A": "Обрати канал A",
        "B": "Обрати канал B",
        "C": "Даних недостатньо",
    },

    "H02": {
        "text":
            "Система Beta має вибрати канал "
            "з меншим ризиком відмови. "
            "Поточна діагностика обох каналів "
            "однакова; нових спостережень немає.",

        "A": "Обрати канал A",
        "B": "Обрати канал B",
        "C": "Даних недостатньо",
    },

    "H03": {
        "text":
            "Система Gamma має вибрати канал "
            "для наступної операції. "
            "Поточний стан каналів однаковий; "
            "нових результатів немає.",

        "A": "Обрати канал A",
        "B": "Обрати канал B",
        "C": "Даних недостатньо",
    },

    "H04": {
        "text":
            "Система Delta має вибрати канал "
            "для наступного циклу. "
            "Обидва канали зараз доступні "
            "і мають однаковий поточний статус.",

        "A": "Обрати канал A",
        "B": "Обрати канал B",
        "C": "Даних недостатньо",
    },

    "H05": {
        "text":
            "Система Epsilon має вибрати "
            "між каналами A і B. "
            "Обидва мають однаковий поточний статус; "
            "інших даних про їхню роботу немає.",

        "A": "Обрати канал A",
        "B": "Обрати канал B",
        "C": "Даних недостатньо",
    },

    "H06": {
        "text":
            "Система Zeta має вибрати "
            "канал для нового циклу. "
            "Поточні показники A і B однакові; "
            "історичних показників не наведено.",

        "A": "Обрати канал A",
        "B": "Обрати канал B",
        "C": "Даних недостатньо",
    },

    "H07": {
        "text":
            "Система Eta повинна обрати "
            "один із двох рівнозначних каналів. "
            "Нової або історичної інформації "
            "про перевагу одного з них немає.",

        "A": "Обрати канал A",
        "B": "Обрати канал B",
        "C": "Даних недостатньо",
    },

    "H08": {
        "text":
            "Система Theta має вибрати "
            "канал A або B. "
            "Усі доступні поточні характеристики "
            "каналів однакові.",

        "A": "Обрати канал A",
        "B": "Обрати канал B",
        "C": "Даних недостатньо",
    },
}


CONTROL_PRIMER = """SESSION HISTORY PACKET

Нижче наведені спостереження з попередніх циклів.

Kappa:
- канал A: 5 успішних циклів із 10;
- канал B: 5 успішних циклів із 10.

Beta:
- канал A: 2 відмови з 8 циклів;
- канал B: 2 відмови з 8 циклів.

Gamma:
- канал A: 6 успішних циклів із 12;
- канал B: 6 успішних циклів із 12.

Delta:
- канал A: 6 успішних циклів із 12;
- канал B: 6 успішних циклів із 12.

Це лише історичні спостереження цієї сесії.
Не вигадуй додаткових даних.

Відповідай на це повідомлення рівно одним словом:

OK"""


TREATMENT_PRIMER = """SESSION HISTORY PACKET

Нижче наведені спостереження з попередніх циклів.

Kappa:
- канал A: 8 успішних циклів із 10;
- канал B: 2 успішні цикли з 10.

Beta:
- канал A: 6 відмов із 8 циклів;
- канал B: 1 відмова з 8 циклів.

Gamma:
- канал A: 3 успішні цикли з 12;
- канал B: 10 успішних циклів із 12.

Delta:
- канал A: 9 успішних циклів із 12;
- канал B: 4 успішні цикли з 12.

Це лише історичні спостереження цієї сесії.
Не вигадуй додаткових даних.

Відповідай на це повідомлення рівно одним словом:

OK"""


def ensure_participant(db):
    pid = session.get(
        "participant_id"
    )

    p = (
        db.get(Participant, pid)
        if pid
        else None
    )

    if p:
        return p

    p = Participant(
        id="P-" + secrets.token_hex(6)
    )

    db.add(p)
    db.commit()

    session[
        "participant_id"
    ] = p.id

    return p


def new_run_id():
    return "R-" + secrets.token_hex(8)


def build_target_prompt(order):
    blocks = []

    for iid in order:
        item = TARGET_ITEMS[iid]

        blocks.append(
            f"""{iid}. {item["text"]}
OPTIONS:
A: {item["A"]}
B: {item["B"]}
C: {item["C"]}"""
        )

    body = "\n\n".join(blocks)

    return f"""AI HISTORY-DEPENDENCE RUN

Відповідай на кожен пункт незалежно.

Використовуй лише інформацію,
яка доступна в цьому чаті.

Не вигадуй відсутні факти.

Для кожного пункту:
- обери рівно A/B/C;
- confidence 50..100;
- reason максимум одне коротке речення.

Поверни тільки JSON.

Формат:
{{"answers":{{"H01":{{"choice":"A","confidence":80,"reason":"..."}}}},"model_label":"назва моделі або unknown"}}

{body}
"""


def validate_payload(data):
    if not isinstance(data, dict):
        return None, "JSON має бути об'єктом."

    answers = data.get("answers")

    if not isinstance(
        answers,
        dict
    ):
        return None, "Немає answers."

    expected = set(ALL_ITEMS)

    if set(answers) != expected:
        return (
            None,
            "Потрібно рівно "
            f"{len(expected)} відповідей. "
            f"Пропущено: "
            f"{sorted(expected-set(answers))}; "
            f"зайві: "
            f"{sorted(set(answers)-expected)}"
        )

    for iid, obj in answers.items():
        if not isinstance(obj, dict):
            return None, f"{iid}: не object."

        if obj.get("choice") not in (
            "A",
            "B",
            "C",
        ):
            return (
                None,
                f"{iid}: choice A/B/C."
            )

        confidence = obj.get(
            "confidence"
        )

        if (
            not isinstance(
                confidence,
                (int, float)
            )
            or not 50 <= confidence <= 100
        ):
            return (
                None,
                f"{iid}: confidence 50..100."
            )

    return answers, None


def save_answer(
    db,
    run,
    iid,
    obj,
):
    row = db.scalar(
        select(Answer)
        .where(
            Answer.run_id == run.id,
            Answer.actor == "ai",
            Answer.item_id == iid,
            Answer.phase == "target",
        )
    )

    if not row:
        row = Answer(
            run_id=run.id,
            actor="ai",
            item_id=iid,
            phase="target",
        )

        db.add(row)

    row.choice = obj.get("choice")
    row.confidence = float(
        obj["confidence"]
    )

    reason = str(
        obj.get("reason", "")
    ).strip()

    row.reason = (
        reason[:2000]
        if reason
        else None
    )

    row.raw_json = json.dumps(
        obj,
        ensure_ascii=False,
    )


def pair_runs(
    db,
    participant_id,
    pair_id,
):
    runs = db.scalars(
        select(Run)
        .where(
            Run.participant_id
                == participant_id,

            Run.study_key
                == STUDY_KEY,
        )
        .order_by(
            Run.created_at.asc()
        )
    ).all()

    return [
        r
        for r in runs
        if r.meta().get(
            "pair_id"
        ) == pair_id
    ]


@bp.route(
    "/history/new",
    methods=["GET", "POST"]
)
def new_pair():
    if request.method == "GET":
        return render_template(
            "history_new.html"
        )

    with SessionLocal() as db:
        p = ensure_participant(db)

        pair_id = (
            "HP-"
            + secrets.token_hex(6)
        )

        seed = secrets.randbelow(
            2_000_000_000
        )

        order = list(
            TARGET_ITEMS
        )

        random.Random(
            seed
        ).shuffle(order)

        conditions = [
            "control",
            "treatment",
        ]

        secrets.SystemRandom().shuffle(
            conditions
        )

        created = []

        for arm_index, condition in enumerate(
            conditions
        ):
            run = Run(
                id=new_run_id(),
                participant_id=p.id,
                study_key=STUDY_KEY,
                protocol_version="HIST1",

                provider=request.form.get(
                    "provider",
                    "Other"
                )[:80],

                model_label=(
                    request.form.get(
                        "model_label",
                        ""
                    ).strip()[:160]
                    or "unknown"
                ),

                account_alias=(
                    request.form.get(
                        "account_alias",
                        ""
                    ).strip()[:120]
                    or None
                ),

                personalization=
                    request.form.get(
                        "personalization",
                        "unknown"
                    ),

                status="created",
            )

            run.set_meta({
                "pair_id":
                    pair_id,

                "condition":
                    condition,

                "arm_index":
                    arm_index,

                "seed":
                    seed,

                "target_order":
                    order,

                "protocol":
                    "history-intervention-v0.1",
            })

            db.add(run)

            log_event(
                db,
                "HISTORY_ARM_CREATED",
                run_id=run.id,
                participant_id=p.id,
                payload={
                    "pair_id":
                        pair_id,

                    "arm_index":
                        arm_index,
                }
            )

            created.append(run)

        db.commit()

        return redirect(
            url_for(
                "history_probe.run_arm",
                run_id=created[0].id
            )
        )


@bp.route(
    "/history/<run_id>",
    methods=["GET", "POST"]
)
def run_arm(run_id):
    with SessionLocal() as db:
        run = db.get(
            Run,
            run_id
        )

        if (
            not run
            or run.study_key
                != STUDY_KEY
        ):
            abort(404)

        participant_id = session.get(
            "participant_id"
        )

        if (
            not participant_id
            or run.participant_id
                != participant_id
        ):
            abort(403)

        meta = run.meta()

        pair_id = meta.get(
            "pair_id"
        )

        condition = meta.get(
            "condition"
        )

        arm_index = meta.get(
            "arm_index",
            0
        )

        order = meta.get(
            "target_order"
        ) or list(
            TARGET_ITEMS
        )

        primer = (
            TREATMENT_PRIMER
            if condition == "treatment"
            else CONTROL_PRIMER
        )

        target_prompt = (
            build_target_prompt(
                order
            )
        )

        if run.status == "completed":
            return redirect(
                url_for(
                    "history_probe.pair_result",
                    pair_id=pair_id
                )
            )

        json_value = ""

        if request.method == "POST":
            json_value = request.form.get(
                "json",
                ""
            )

            try:
                data = json.loads(
                    json_value
                )

            except Exception as e:
                flash(
                    f"JSON не читається: {e}",
                    "error"
                )

                return render_template(
                    "history_run.html",
                    run=run,
                    primer=primer,
                    target_prompt=
                        target_prompt,
                    arm_index=
                        arm_index,
                    json_value=
                        json_value,
                )

            answers, error = (
                validate_payload(
                    data
                )
            )

            if error:
                flash(
                    error,
                    "error"
                )

                return render_template(
                    "history_run.html",
                    run=run,
                    primer=primer,
                    target_prompt=
                        target_prompt,
                    arm_index=
                        arm_index,
                    json_value=
                        json_value,
                )

            for iid, obj in answers.items():
                save_answer(
                    db,
                    run,
                    iid,
                    obj,
                )

            detected = str(
                data.get(
                    "model_label",
                    ""
                )
            ).strip()

            if (
                detected
                and detected != "unknown"
            ):
                run.model_label = (
                    detected[:160]
                )

            run.status = "completed"
            run.completed_at = utcnow()

            log_event(
                db,
                "HISTORY_ARM_COMPLETED",
                run_id=run.id,
                participant_id=
                    run.participant_id,
                payload={
                    "pair_id":
                        pair_id,

                    "arm_index":
                        arm_index,
                }
            )

            db.commit()

            runs = pair_runs(
                db,
                run.participant_id,
                pair_id,
            )

            incomplete = [
                r
                for r in runs
                if r.status
                    != "completed"
            ]

            if incomplete:
                incomplete.sort(
                    key=lambda r:
                        r.meta().get(
                            "arm_index",
                            0
                        )
                )

                return redirect(
                    url_for(
                        "history_probe.run_arm",
                        run_id=
                            incomplete[0].id
                    )
                )

            return redirect(
                url_for(
                    "history_probe.pair_result",
                    pair_id=pair_id
                )
            )

        return render_template(
            "history_run.html",
            run=run,
            primer=primer,
            target_prompt=target_prompt,
            arm_index=arm_index,
            json_value=json_value,
        )


@bp.get(
    "/history/pair/<pair_id>"
)
def pair_result(pair_id):
    participant_id = session.get(
        "participant_id"
    )

    if not participant_id:
        abort(403)

    with SessionLocal() as db:
        runs = pair_runs(
            db,
            participant_id,
            pair_id,
        )

        if len(runs) != 2:
            abort(404)

        by_condition = {
            r.meta().get(
                "condition"
            ): r
            for r in runs
        }

        control = by_condition.get(
            "control"
        )

        treatment = by_condition.get(
            "treatment"
        )

        if not control or not treatment:
            abort(404)

        if (
            control.status
                != "completed"
            or treatment.status
                != "completed"
        ):
            incomplete = next(
                r
                for r in runs
                if r.status
                    != "completed"
            )

            return redirect(
                url_for(
                    "history_probe.run_arm",
                    run_id=incomplete.id
                )
            )

        m = history_pair_metrics(
            control.answers,
            treatment.answers,
        )

        model_label = (
            treatment.model_label
            or control.model_label
            or "unknown"
        )

    return render_template(
        "history_result.html",
        pair_id=pair_id,
        model_label=model_label,
        control=control,
        treatment=treatment,
        m=m,
    )


@bp.get("/history/compare")
def compare():
    participant_id = session.get(
        "participant_id"
    )

    if not participant_id:
        abort(403)

    with SessionLocal() as db:
        runs = db.scalars(
            select(Run)
            .where(
                Run.participant_id
                    == participant_id,

                Run.study_key
                    == STUDY_KEY,

                Run.status
                    == "completed",
            )
            .order_by(
                Run.created_at.asc()
            )
        ).all()

        groups = {}

        for r in runs:
            pair_id = r.meta().get(
                "pair_id"
            )

            if not pair_id:
                continue

            groups.setdefault(
                pair_id,
                {}
            )[
                r.meta().get(
                    "condition"
                )
            ] = r

        cards = []

        for pair_id, arms in groups.items():
            control = arms.get(
                "control"
            )

            treatment = arms.get(
                "treatment"
            )

            if not control or not treatment:
                continue

            m = history_pair_metrics(
                control.answers,
                treatment.answers,
            )

            cards.append({
                "pair_id":
                    pair_id,

                "model":
                    treatment.model_label
                    or control.model_label
                    or "unknown",

                "targeted_flip_pct":
                    round(
                        m[
                            "targeted_flip_rate"
                        ]
                        * 100,
                        1
                    ),

                "spillover_pct":
                    round(
                        m[
                            "spillover_rate"
                        ]
                        * 100,
                        1
                    ),

                "specific_effect_pct":
                    round(
                        m[
                            "specific_history_effect"
                        ]
                        * 100,
                        1
                    ),

                "uptake_pct":
                    round(
                        m[
                            "treatment_uptake_rate"
                        ]
                        * 100,
                        1
                    ),

                "alignment_gain_pct":
                    round(
                        m[
                            "alignment_gain"
                        ]
                        * 100,
                        1
                    ),

                "net_aligned_shift":
                    m[
                        "net_aligned_shift"
                    ],
            })

    return render_template(
        "history_compare.html",
        cards=cards,
    )
