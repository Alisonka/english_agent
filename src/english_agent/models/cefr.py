"""The CEFR level type, shared across the curriculum, lessons and the learner state.

A leaf module (no project imports) so it can be used from `paths`, `config` and
the models without import cycles.
"""

from __future__ import annotations

from typing import Literal, get_args

Cefr = Literal["A2", "B1", "B2", "C1"]

# Declaration order is the learning order; used when offering levels to the user.
CEFR_LEVELS: tuple[Cefr, ...] = get_args(Cefr)
