"""Grading of a benchmark answer against the planted list (M3 T4, D35).

``extract_items`` pulls the agent's final answer out of its text: the last fenced ``json`` block
(prose around it is fine), ``{"items": [{"kind": ..., "ref": ...}, ...]}``. A missing or malformed
block gives no items and ``malformed=True``. ``normalise`` makes references comparable, and
``grade`` scores the reported items against the answer key:

- an item is a ``(kind, ref)`` pair; the pair is compared after normalisation (kind lower-cased;
  ref stripped and upper-cased, and reduced to its digits when it is a number that may carry a
  prefix such as ``PO 4500123`` or ``#4500123``);
- duplicates count once; an item whose kind is not one of ``email | po | portal | system`` (or that
  could not be read as an object with ``kind`` and ``ref``) is a false positive;
- ``recall`` = planted found / planted; ``recall_observable`` = the same over the planted items
  that Since can observe at all (the replay says which); ``recall_observable_a`` = the same over
  the planted items that arm A's raw tools can reach (``arm_a_observable``); ``precision`` =
  correct reported / reported. With nothing reported, precision is 0.0: nothing correct was
  delivered.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from bench.world import KIND_PORTAL, KIND_SYSTEM, NOW, SYSTEM_LAYOUT, World

KINDS = ("email", "po", "portal", "system")
UNREADABLE_KIND = (
    "?"  # an entry that is not an object with kind and ref: reported, but a false positive
)

Item = tuple[str, str]

_FENCE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\r?\n?(.*?)```", re.DOTALL)
_NUMBER_REF = re.compile(r"\D*?(\d+)\D*")


class Extraction(NamedTuple):
    """The items as reported (raw strings, not yet normalised). ``malformed`` is set when there is
    no usable JSON answer; ``note`` says what was wrong (empty when the answer was readable)."""

    items: list[Item]
    malformed: bool
    note: str


def _json_answer(text: str) -> tuple[str | None, str]:
    """The text of the answer block: the last ``json`` fenced block (or an untagged fenced block
    that looks like JSON); failing that, the last bare JSON object holding ``"items"``."""
    blocks = []
    for match in _FENCE.finditer(text):
        lang, body = match.group(1).lower(), match.group(2).strip()
        if lang == "json" or (lang == "" and body[:1] in ("{", "[")):
            blocks.append(body)
    if blocks:
        return blocks[-1], ""
    decoder = json.JSONDecoder()
    for start in range(len(text) - 1, -1, -1):
        if text[start] != "{":
            continue
        try:
            value, end = decoder.raw_decode(text, start)
        except ValueError:
            continue
        if isinstance(value, dict) and "items" in value:
            return text[start:end], ""
    return None, "no json block found"


def _entry(entry: Any) -> Item:
    if isinstance(entry, dict) and "kind" in entry and "ref" in entry:
        kind, ref = entry["kind"], entry["ref"]
        if isinstance(kind, str) and isinstance(ref, (str, int)) and not isinstance(ref, bool):
            return kind, str(ref)
    return UNREADABLE_KIND, json.dumps(entry, sort_keys=True, ensure_ascii=True)[:80]


def extract_items(text: str) -> Extraction:
    """The reported items of an answer text (see the module docstring)."""
    block, problem = _json_answer(text or "")
    if block is None:
        return Extraction([], True, problem)
    try:
        data = json.loads(block)
    except ValueError as exc:
        return Extraction([], True, f"invalid json: {exc}")
    entries = data.get("items") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return Extraction([], True, 'json has no "items" list')
    return Extraction([_entry(entry) for entry in entries], False, "")


def normalise(kind: Any, ref: Any) -> Item:
    """``(kind, ref)`` in comparable form. Numeric references (a single run of digits, possibly with
    a prefix or a ``#``) become the bare digits; system references (``portal-login``) get their
    separators turned into ``-``; anything else is just stripped and upper-cased."""
    norm_kind = str(kind).strip().lower()
    text = " ".join(str(ref).split())
    if norm_kind == "system":
        return norm_kind, re.sub(r"[\s_]+", "-", text).upper()
    if norm_kind in KINDS:
        number = _NUMBER_REF.fullmatch(text)
        if number:
            return norm_kind, number.group(1)
    return norm_kind, text.upper()


@dataclass(frozen=True)
class Grade:
    """``tp`` / ``fn`` list planted items as written in the answer key; ``fp`` lists reported items
    in normalised form. ``reported`` counts distinct reported items, ``duplicates`` the repeats."""

    recall: float
    recall_observable: float
    recall_observable_a: float
    precision: float
    tp: list[Item] = field(default_factory=list)
    fp: list[Item] = field(default_factory=list)
    fn: list[Item] = field(default_factory=list)
    reported: int = 0
    duplicates: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "recall": self.recall,
            "recall_observable": self.recall_observable,
            "recall_observable_a": self.recall_observable_a,
            "precision": self.precision,
            "tp": [list(item) for item in self.tp],
            "fp": [list(item) for item in self.fp],
            "fn": [list(item) for item in self.fn],
            "reported": self.reported,
            "duplicates": self.duplicates,
        }


def _ratio(found: int, total: int, empty: float) -> float:
    return found / total if total else empty


# The same reasoning as in ``arm_a_observable``, for the report.
ARM_A_LIMITS = (
    "A cannot see any portal order change (the portal is behind a login at the simulated now, "
    "and A's notes are from the last look) and cannot detect portal-layout (the changed page is "
    "behind the login page too)"
)


def arm_a_observable(planted: Iterable[Item], *, portal_blind: bool = True) -> list[Item]:
    """The planted items that arm A's raw tools can reach at all.

    Arm A reads the mailbox and the PO database as they are now, and holds notes of the PO table
    and the portal page from the last look. So it can find every mail item and (by diffing the
    notes against the database) every PO item. The portal is different: ``fetch_portal()`` returns
    the page as of now, and when the portal's login has expired by now (``portal_blind``) that page
    is the login page. Then

    - no portal order change is visible: the notes show the orders as of the last look, the page
      shows none of them, and no other tool reaches the portal;
    - ``portal-layout`` cannot be detected: the page that changed layout is behind the login page,
      and the notes are from before the change;
    - ``portal-login`` is visible: the login page is the evidence.

    When the portal is readable at now (``portal_blind=False``) A sees the page, so every item is
    reachable. Order is that of ``planted``."""
    layout = (KIND_SYSTEM, SYSTEM_LAYOUT)
    return [
        (kind, ref)
        for kind, ref in planted
        if not (portal_blind and (kind == KIND_PORTAL or (kind, ref) == layout))
    ]


def arm_a_observable_in(world: World) -> list[Item]:
    """``arm_a_observable`` of a world: A is blind to the portal exactly when the portal's login
    has expired by ``NOW`` (the page A can fetch is then the login page)."""
    blind = world.state_at(NOW).portal.login_expired
    return arm_a_observable(world.planted, portal_blind=blind)


def grade(
    items: Iterable[Item],
    planted: Iterable[Item],
    observable: Iterable[Item],
    observable_a: Iterable[Item],
) -> Grade:
    """Score ``items`` (as reported) against ``planted`` (the answer key), ``observable`` (the part
    of it Since can see) and ``observable_a`` (the part arm A's tools can reach). Everything is
    compared after ``normalise``."""
    key = {normalise(kind, ref): (kind, ref) for kind, ref in planted}
    seen_observable = {normalise(kind, ref) for kind, ref in observable} & key.keys()
    seen_a = {normalise(kind, ref) for kind, ref in observable_a} & key.keys()
    normalised = [normalise(kind, ref) for kind, ref in items]
    reported = set(normalised)
    correct = reported & key.keys()
    return Grade(
        recall=_ratio(len(correct), len(key), 1.0),
        recall_observable=_ratio(len(correct & seen_observable), len(seen_observable), 1.0),
        recall_observable_a=_ratio(len(correct & seen_a), len(seen_a), 1.0),
        precision=_ratio(len(correct), len(reported), 0.0),
        tp=[key[item] for item in sorted(correct)],
        fp=sorted(item for item in reported if item not in key),
        fn=[key[item] for item in sorted(key.keys() - reported)],
        reported=len(reported),
        duplicates=len(normalised) - len(reported),
    )
