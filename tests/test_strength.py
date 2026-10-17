import pytest

from app.strength import (
    BAR_KG,
    DEFAULT_FORMULA,
    MAX_REPS,
    LiftError,
    better_set,
    brzycki,
    compare,
    deload,
    epley,
    estimates,
    formulas_agree,
    is_personal_best,
    one_rep_max,
    plateau,
    round_to_plates,
    session_best,
    volume,
    warmup_sets,
)


def test_formulas_agree_on_five_vs_three():
    # 100kg x 5 -> Epley 116.7, Brzycki 112.5
    # 110kg x 3 -> Epley 121.0, Brzycki 116.5
    assert round(epley(100, 5), 1) == 116.7
    assert round(brzycki(100, 5), 1) == 112.5
    assert round(epley(110, 3), 1) == 121.0
    assert round(brzycki(110, 3), 1) == 116.5

    a = (100, 5)
    b = (110, 3)
    assert formulas_agree(a, b) is True
    assert compare(a, b, "epley")["better"] == "b"
    assert compare(a, b, "brzycki")["better"] == "b"


def test_formulas_disagree_on_crossover_pair():
    # 100kg x 12 -> Epley 140.0, Brzycki 144.0
    # 135kg x 2  -> Epley 144.0, Brzycki 138.9
    assert round(epley(100, 12), 1) == 140.0
    assert round(brzycki(100, 12), 1) == 144.0
    assert round(epley(135, 2), 1) == 144.0
    assert round(brzycki(135, 2), 1) == 138.9

    a = (100, 12)
    b = (135, 2)
    assert formulas_agree(a, b) is False
    assert compare(a, b, "epley")["better"] == "b"
    assert compare(a, b, "brzycki")["better"] == "a"


def test_single_rep_returns_exact_weight():
    for w in (50, 100.5, 142.5):
        assert epley(w, 1) == w
        assert brzycki(w, 1) == w
        assert one_rep_max(w, 1, "epley") == w
        assert one_rep_max(w, 1, "brzycki") == w


def test_crossover_at_ten_reps():
    # At reps=10:
    # Epley: w * (1 + 10/30) = w * 4/3
    # Brzycki: w * 36 / (37 - 10) = w * 36/27 = w * 4/3
    for w in (60, 90, 120):
        assert epley(w, 10) == brzycki(w, 10)
        assert one_rep_max(w, 10, "epley") == one_rep_max(w, 10, "brzycki")


def test_input_validation_errors():
    with pytest.raises(LiftError, match="must be greater than zero"):
        epley(0, 5)
    with pytest.raises(LiftError, match="must be greater than zero"):
        brzycki(-10, 5)
    with pytest.raises(LiftError, match="at least one rep"):
        epley(100, 0)
    with pytest.raises(LiftError, match="at least one rep"):
        brzycki(100, -2)
    with pytest.raises(LiftError, match="is not a set"):
        epley("abc", 5)

    # Max reps boundary
    assert epley(100, MAX_REPS) > 0
    with pytest.raises(LiftError, match=f"past {MAX_REPS}"):
        epley(100, MAX_REPS + 1)
    with pytest.raises(LiftError, match=f"past {MAX_REPS}"):
        brzycki(100, 13)


def test_unknown_formula():
    with pytest.raises(LiftError, match="unknown formula"):
        one_rep_max(100, 5, formula="nonexistent")


def test_compare_antisymmetry_and_ties():
    a = {"weight": 100, "reps": 5}
    b = {"weight": 110, "reps": 3}
    res_ab = compare(a, b, DEFAULT_FORMULA)
    res_ba = compare(b, a, DEFAULT_FORMULA)

    assert res_ab["better"] == "b"
    assert res_ba["better"] == "a"
    assert res_ab["margin"] == res_ba["margin"]

    # Tie within 0.1kg margin
    tie_res = compare((100, 5), (100, 5))
    assert tie_res["better"] == "tie"
    assert tie_res["margin"] == 0.0

    # better_set helper
    assert better_set(a, b, DEFAULT_FORMULA) == b
    assert better_set(b, a, DEFAULT_FORMULA) == b
    # On tie, keeps first
    assert better_set((100, 5), (100, 5)) == (100, 5)


def test_estimates_helper():
    est = estimates(100, 5)
    assert est["weight"] == 100.0
    assert est["reps"] == 5
    assert est["epley"] == 116.67
    assert est["brzycki"] == 112.50
    assert est["spread"] == 4.17
    assert est["agree_within_kg"] is False


def test_personal_best_logic():
    # First set ever
    pb1 = is_personal_best(100, 5, None)
    assert pb1["personal_best"] is True
    assert pb1["heaviest_ever"] is True
    assert pb1["previous_e1rm"] is None

    # Stored PB is 100x5 (e1rm ~ 116.67)
    stored = {"weight": 100, "reps": 5, "e1rm": 116.67}

    # Set beating e1rm with heavier weight
    pb2 = is_personal_best(110, 5, stored)
    assert pb2["personal_best"] is True
    assert pb2["heaviest_ever"] is True
    assert pb2["gain"] > 0

    # Set failing to beat e1rm
    pb3 = is_personal_best(90, 5, stored)
    assert pb3["personal_best"] is False
    assert pb3["heaviest_ever"] is False
    assert pb3["gain"] < 0

    # Heaviest bar moved (105x1 = 105), but e1rm (105) does not beat 116.67
    pb4 = is_personal_best(105, 1, stored)
    assert pb4["personal_best"] is False
    assert pb4["heaviest_ever"] is True


def test_session_best_and_volume():
    sets = [(100, 5), (105, 3), (90, 8)]
    best = session_best(sets, "epley")
    assert best == 116.67  # 105x3 is 115.5, 90x8 is 114.0, 100x5 is 116.67

    vol = volume(sets)
    # (100*5) + (105*3) + (90*8) = 500 + 315 + 720 = 1535
    assert vol == 1535.0


def test_plateau_detection():
    # Window < 1 raises
    with pytest.raises(LiftError, match="window must be at least one"):
        plateau([100, 105], window=0)

    # Too few sessions to judge
    short = plateau([100, 102], window=3)
    assert short["stalled"] is False
    assert "only 2 sessions" in short["reason"]

    # Stalled series (no gain over tolerance 0.5)
    # Earlier peak = 100, recent peak = 100.2 (gain 0.2 <= 0.5)
    stalled = plateau([95, 100, 98, 100.2, 99], window=3, tolerance=0.5)
    assert stalled["stalled"] is True
    assert stalled["best_before"] == 100.0
    assert stalled["best_recent"] == 100.2

    # Progressing series
    # Earlier peak = 100, recent peak = 102.5 (gain 2.5 > 0.5)
    progressing = plateau([95, 100, 98, 101, 102.5], window=3, tolerance=0.5)
    assert progressing["stalled"] is False
    assert progressing["gain"] == 2.5


def test_round_to_plates():
    # Less than or equal to empty bar
    bar_only = round_to_plates(15.0)
    assert bar_only["weight"] == BAR_KG
    assert bar_only["per_side"] == []

    # Exact plate match: 60kg = 20kg bar + 20kg each side
    sixty = round_to_plates(60.0)
    assert sixty["weight"] == 60.0
    assert sixty["per_side"] == [20.0]
    assert sixty["exact"] is True

    # Inexact weight: 83kg -> 82.5kg (20 + 2*(25 + 5 + 1.25))
    eighty_three = round_to_plates(83.0)
    assert eighty_three["weight"] == 82.5
    assert eighty_three["exact"] is False


def test_deload_and_warmup_ladder():
    with pytest.raises(LiftError, match="must be greater than zero"):
        deload(0)

    # 100kg deload at 90% = 90kg
    assert deload(100) == 90.0

    with pytest.raises(LiftError, match="must be greater than zero"):
        warmup_sets(0)

    ladder = warmup_sets(100.0)
    assert len(ladder) >= 3
    assert ladder[0]["note"] == "empty bar"
    assert ladder[0]["weight"] == 20.0
    assert ladder[-1]["note"] == "working sets"
    assert ladder[-1]["weight"] == 100.0
