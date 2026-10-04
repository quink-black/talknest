"""Per-turn teaching plan.

New words come from the current unit. The parent picks that unit; when they
have not, the first unit that still has untaught words wins, which walks the
curriculum in dependency order on its own. Once every unit has been
introduced the weakest one is revisited instead.
"""

from __future__ import annotations

from . import state_block
from .store import Store

CURRENT_UNIT = "current_unit"
# A word shown this many times without the child saying it stops being
# offered as new today; it comes back through review.
MAX_EXPOSURES = 3
# Every PLAN_EVERY-th turn works the plan's words into the talk. The turns
# in between follow the child's topic and teach a word from it: a model
# handed an unrelated word every turn drags it into whatever is being said,
# and a word that does not belong makes the conversation feel scripted.
PLAN_EVERY = 3
# A unit is practised in sentences once this share of its words has been
# said on more than one day.
SENTENCE_SHARE = 0.5


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
    return weakest_unit(store, units)


def weakest_unit(store: Store, units: list):
    """The unit whose words sit in the lowest boxes on average.

    With nothing left to introduce the lesson goes back to the least secure
    unit, rather than to whichever unit happens to come last.
    """
    if not units:
        return None
    boxes = store.boxes()
    return min(units, key=lambda u: sum(boxes.get(w, 0) for w in u.words)
               / len(u.words))


def plan(store: Store, units: list, caps: state_block.Caps,
         portrait=None, session_id: int | None = None, opening: bool = False,
         new_per_day: int = 5, turn: int | None = None,
         at: str | None = None) -> state_block.Plan:
    """What the tutor may teach this turn.

    turn counts the child's turns so far in this conversation. Most turns
    follow the child's topic and carry no words from the plan; every
    PLAN_EVERY-th one brings the plan's words in. Without a turn count
    every turn is a plan turn.

    portrait, when the learner has one, contributes the newest lines of what
    is known about them and what the last session covered. opening marks the
    first turn of a conversation, which gets topics to start from.
    """
    unit = current_unit(store, units)
    if unit is None:
        return state_block.Plan()
    digest = list(portrait.recent[-caps.digest_lines:]) if portrait else []
    if not digest and session_id is not None:
        digest = store.last_topics(session_id)[: caps.digest_lines]
    if turn is not None and not plan_turn(turn):
        return state_block.Plan(
            unit=unit.id, title=unit.title, digest=digest,
            portrait=(list(portrait.notes[-caps.portrait_lines:])
                      if portrait else []),
            topics=list(unit.topics[:2]) if opening else [],
            hint=hint([], [], "", opening, topic_turn=True))
    new = new_words(store, unit, caps.new_items, new_per_day, at)
    review = store.review_due(caps.review_items, at)
    # A word already picked for review is not listed as known as well: the
    # block is short, and the same word twice is one hint too many.
    known = [w for w in store.known_in_units(window(units, unit.id),
                                             caps.known_items + len(review))
             if w not in review and w not in new][: caps.known_items]
    notes = {w: n for w, n in store.topic_words().items() if n}
    secure = sentence_ready(store, unit)
    pattern = ""
    if secure and unit.patterns:
        pattern = unit.patterns[(session_id or 0) % len(unit.patterns)]
    return state_block.Plan(
        unit=unit.id, new=new, review=review, known=known,
        portrait=list(portrait.notes[-caps.portrait_lines:]) if portrait else [],
        digest=digest, title=unit.title, pattern=pattern,
        topics=list(unit.topics[:2]) if opening else [], notes=notes,
        hint=hint(new, review, pattern, opening),
    )


def new_words(store: Store, unit, limit: int, per_day: int,
              at: str | None = None) -> list[str]:
    """Words to introduce this turn.

    Words introduced today that the child has not yet said come first and
    block anything newer: the next word is offered only once the child has
    produced one. A daily cap keeps a long sitting from turning the whole
    curriculum over in an afternoon.
    """
    waiting = store.pending(limit, MAX_EXPOSURES, at)
    if waiting:
        return waiting
    room = per_day - store.introduced_today(at)
    return store.new_in_unit(unit.id, min(limit, room)) if room > 0 else []


def sentence_ready(store: Store, unit) -> bool:
    """Whether enough of the unit is secure to practise it in sentences."""
    boxes = store.boxes()
    secure = sum(1 for w in unit.words if boxes.get(w, 0) >= 2)
    return secure >= SENTENCE_SHARE * len(unit.words)


def plan_turn(turn: int) -> bool:
    return turn % PLAN_EVERY == PLAN_EVERY - 1


def hint(new: list[str], review: list[str], pattern: str,
         opening: bool, topic_turn: bool = False) -> str:
    """The single suggestion for this turn, in the order of what matters.

    One instruction is easier for the model to follow than a list, and the
    state already decided which one applies.
    """
    if opening:
        return "用上次的话题或下面的话题和孩子聊起来，不要宣布今天要复习"
    if topic_turn:
        return ("接住孩子的话题，只教话题里的一个简单词，"
                "这一轮不用计划里的词")
    if new:
        return (f"话题里自然用得上 {new[0]} 就教它，用不上就教话题里的"
                "一个简单词，不要硬塞")
    if pattern:
        return "顺着孩子的话题，用句型让孩子说一句完整的话"
    if review:
        return (f"话题里自然用得上 {review[0]} 就用一下，用不上就教话题里的"
                "一个简单词")
    return "顺着孩子的话题聊，教话题里的一个简单词"


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
