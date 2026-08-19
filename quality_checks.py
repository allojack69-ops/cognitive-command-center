
cd ~/cognitive-command-center

cat > quality_checks.py <<'PY'
import re


# Це НЕ моральний "gold score".
# Тут лише формальні пункти, де постановка тесту задає
# перевірюваний логічний / математичний результат.
OBJECTIVE_RULES = [
    {
        "key": "PX1_BASE",
        "item_id": "PX1",
        "phase": "base",
        "label": "Логічна суперечність",
        "expected_choice": "C",
        "expected_frame": "CONTRADICTORY",
    },
    {
        "key": "PX2_BASE",
        "item_id": "PX2",
        "phase": "base",
        "label": "Недостатньо даних про опціонність",
        "expected_choice": "C",
        "expected_frame": "UNDERSPECIFIED",
    },
    {
        "key": "PX2_UPDATE",
        "item_id": "PX2",
        "phase": "update",
        "label": "Числове розв'язання: 21.6% vs 64%",
        "expected_choice": "B",
        "expected_frame": "ACCEPT",
    },
]


# Навмисно консервативно:
# ми НЕ намагаємось "читати думки" моделі.
# Позначаємо conclusion лише якщо reason буквально містить
# явний висновок A/B/C.
EXPLICIT_CONCLUSION_PATTERNS = [
    re.compile(
        r"(?:отже|тож|висновок)\s*[:\-–—,]?\s*"
        r"(?:варіант\s*)?([ABC])\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:обираю|оберу|мій\s+вибір|правильна\s+відповідь)"
        r"\s*[:\-–—,]?\s*(?:варіант\s*)?([ABC])\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:therefore|thus)\s*[:\-–—,]?\s*"
        r"(?:option\s*)?([ABC])\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:i\s+choose|choice\s+is|answer\s+is|"
        r"correct\s+answer\s+is)\s*[:\-–—,]?\s*"
        r"(?:option\s*)?([ABC])\b",
        re.IGNORECASE,
    ),
]


def explicit_reason_choice(reason):
    """
    Повертає A/B/C тільки якщо reason містить явний текстовий висновок.
    Інакше None. Це conservative detector, не semantic judge.
    """
    text = (reason or "").strip()

    if not text:
        return None

    found = []

    for pattern in EXPLICIT_CONCLUSION_PATTERNS:
        for match in pattern.finditer(text):
            found.append((match.start(), match.group(1).upper()))

    if not found:
        return None

    # Якщо висновків кілька — беремо останній явний.
    found.sort(key=lambda x: x[0])
    return found[-1][1]


def evaluate_bot_quality(base_map, update_map):
    rows = []

    choice_correct = 0
    frame_correct = 0
    present = 0

    coherence_checked = 0
    coherence_mismatches = 0

    for rule in OBJECTIVE_RULES:
        amap = base_map if rule["phase"] == "base" else update_map
        answer = amap.get(rule["item_id"])

        actual_choice = answer.choice if answer else None
        actual_frame = answer.frame_status if answer else None
        reason = answer.reason if answer else None

        choice_ok = (
            actual_choice == rule["expected_choice"]
            if actual_choice is not None
            else None
        )

        frame_ok = (
            actual_frame == rule["expected_frame"]
            if actual_frame is not None
            else None
        )

        reason_choice = explicit_reason_choice(reason)

        coherence = None

        if reason_choice is not None and actual_choice is not None:
            coherence_checked += 1
            coherence = reason_choice == actual_choice

            if not coherence:
                coherence_mismatches += 1

        if answer:
            present += 1

        if choice_ok is True:
            choice_correct += 1

        if frame_ok is True:
            frame_correct += 1

        rows.append(
            {
                "key": rule["key"],
                "item_id": rule["item_id"],
                "phase": rule["phase"],
                "label": rule["label"],

                "expected_choice": rule["expected_choice"],
                "expected_frame": rule["expected_frame"],

                "actual_choice": actual_choice or "—",
                "actual_frame": actual_frame or "—",

                "choice_ok": choice_ok,
                "frame_ok": frame_ok,

                "reason_choice": reason_choice,
                "coherence": coherence,
                "reason": reason or "",
            }
        )

    return {
        "total": len(OBJECTIVE_RULES),
        "present": present,

        "choice_correct": choice_correct,
        "frame_correct": frame_correct,

        "coherence_checked": coherence_checked,
        "coherence_mismatches": coherence_mismatches,

        "rows": rows,
    }
