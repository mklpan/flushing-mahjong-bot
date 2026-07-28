"""
Hong Kong / Cantonese mahjong scoring logic ("half-spicy" table).

House rules encoded here:
- Minimum 3 faan to win
- Maximum 13 faan (capped)
- False win: caller pays out at the 4-faan discard rate to each of the
  other 3 players
- 7 pairs is treated as a valid hand type (not a special faan value) --
  the caller enters whatever faan the hand actually scores
"""

MIN_FAAN = 3
MAX_FAAN = 13

# faan: (discard_points, self_draw_total, self_draw_each_opponent_pays)
FAAN_TABLE = {
    0: (1, None, None),
    1: (2, 3, 1),
    2: (4, 6, 2),
    3: (8, 12, 4),
    4: (16, 24, 8),
    5: (24, 36, 12),
    6: (32, 48, 16),
    7: (48, 72, 24),
    8: (64, 96, 32),
    9: (96, 144, 48),
    10: (128, 192, 64),
    11: (192, 288, 96),
    12: (256, 384, 128),
    13: (384, 576, 192),
}

FALSE_WIN_FAAN = 4  # false win payout is fixed at the 4-faan discard rate


class ScoringError(ValueError):
    pass


def clamp_faan(faan: int) -> int:
    if faan > MAX_FAAN:
        return MAX_FAAN
    return faan


def validate_win_faan(faan: int):
    if faan < MIN_FAAN:
        raise ScoringError(
            f"Faan must be at least {MIN_FAAN} to win (got {faan})."
        )
    if faan > MAX_FAAN:
        raise ScoringError(
            f"Faan cannot exceed {MAX_FAAN} (got {faan})."
        )


def score_discard_win(faan: int):
    """Returns dict of {role: points_delta} for a discard win.

    winner: +discard_points
    discarder: -discard_points
    other two players: 0
    """
    validate_win_faan(faan)
    discard_points, _, _ = FAAN_TABLE[faan]
    return {
        "winner": discard_points,
        "discarder": -discard_points,
    }


def score_self_draw_win(faan: int):
    """Returns dict of {role: points_delta} for a self-draw win.

    winner: +self_draw_total
    each of other 3 players: -self_draw_each_opponent_pays
    """
    validate_win_faan(faan)
    _, self_draw_total, each_pays = FAAN_TABLE[faan]
    return {
        "winner": self_draw_total,
        "each_opponent": -each_pays,
    }


def score_false_win():
    """False win: caller pays 4-faan discard rate to each of 3 opponents."""
    discard_points, _, _ = FAAN_TABLE[FALSE_WIN_FAAN]
    return {
        "false_winner": -discard_points * 3,
        "each_opponent": discard_points,
    }
