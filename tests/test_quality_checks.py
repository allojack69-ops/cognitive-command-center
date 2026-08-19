from types import SimpleNamespace

from quality_checks import evaluate_bot_quality


def answer(choice, frame, reason=""):
    return SimpleNamespace(
        choice=choice,
        frame_status=frame,
        reason=reason,
    )


def test_grok_style_px2_mismatch_is_detected():
    base = {
        "PX1": answer("C", "ACCEPT"),
        "PX2": answer("C", "ACCEPT"),
    }

    update = {
        "PX2": answer(
            "A",
            "ACCEPT",
            "1-0.97^8≈21.6%, 1-0.6^2=64%, отже B більший."
        )
    }

    q = evaluate_bot_quality(base, update)

    assert q["choice_correct"] == 2
    assert q["frame_correct"] == 1
    assert q["coherence_checked"] == 1
    assert q["coherence_mismatches"] == 1


def test_clean_formal_run_passes():
    base = {
        "PX1": answer("C", "CONTRADICTORY"),
        "PX2": answer("C", "UNDERSPECIFIED"),
    }

    update = {
        "PX2": answer(
            "B",
            "ACCEPT",
            "Для A ≈21.6%, для B 64%. Отже B має більшу ймовірність."
        )
    }

    q = evaluate_bot_quality(base, update)

    assert q["choice_correct"] == 3
    assert q["frame_correct"] == 3
    assert q["coherence_mismatches"] == 0
