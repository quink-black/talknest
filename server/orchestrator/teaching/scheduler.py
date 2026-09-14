"""Per-turn teaching plan.

New words come from the current unit. The parent picks that unit; when they
have not, the first unit that still has untaught words wins, which walks the
curriculum in dependency order on its own.
"""

from __future__ import annotations

from . import state_block
from .store import Store

CURRENT_UNIT = "current_unit"


def current_unit(store: Store, units: list):
    """The unit new words are drawn from, or None without a curriculum.

    A unit the parent picked stays picked even after every word in it has
    been met: staying on one unit or skipping ahead is their call.
    """
    picked = store.get_setting(CURRENT_UNIT)
    if picked:
        for unit in units:
            if unit.id == picked:
                return unit
    for unit in units:
        if store.new_in_unit(unit.id, 1):
            return unit
    return units[-1] if units else None


def plan(store: Store, units: list, caps: state_block.Caps,
         portrait=None) -> state_block.Plan:
    """What the tutor may teach this turn.

    portrait, when the learner has one, contributes the newest lines of what
    is known about them and what the last session covered.
    """
    unit = current_unit(store, units)
    if unit is None:
        return state_block.Plan()
    new = store.new_in_unit(unit.id, caps.new_items)
    review = store.review_due(caps.review_items)
    # A word already picked for review is not listed as known as well: the
    # block is short, and the same word twice is one hint too many.
    known = [w for w in store.known_in_units(window(units, unit.id),
                                             caps.known_items + len(review))
             if w not in review][: caps.known_items]
    return state_block.Plan(
        unit=unit.id, new=new, review=review, known=known,
        portrait=list(portrait.notes[-caps.portrait_lines:]) if portrait else [],
        digest=list(portrait.recent[-caps.digest_lines:]) if portrait else [],
    )


def window(units: list, unit_id: str) -> list[str]:
    """The current unit and the two before it.

    Restricting the known list keeps it short once a learner is dozens of
    words in, and those are the words the tutor is most likely to bring up
    again by accident.
    """
    ids = [u.id for u in units]
    if unit_id not in ids:
        return [unit_id]
    end = ids.index(unit_id) + 1
    return ids[max(0, end - 3): end]
