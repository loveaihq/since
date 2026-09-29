"""The task prompt of the benchmark (M3 T4, D33 / D35).

One shared text for both arms: who the agent is, the two moments in time, the four rules that
define "needing attention" (so grading is objective), and the required shape of the answer. Only
the paragraph about the agent's tools differs between the arms, and it says nothing beyond which
tools there are.
"""

from __future__ import annotations

from datetime import datetime

from bench.world import ETA_SLIP_DAYS, LAST_LOOK, NOW

ARMS = ("A", "B")

TOOLS = {
    "A": (
        "Your tools give you read access to the company mailbox, the purchase order database, "
        "the customer portal page as it is right now, and the notes you saved at your last look."
    ),
    "B": 'Your tools are the Since tools. Use agent_id="bench" when you call them.',
}

ANSWER_TEMPLATE = (
    '{"items": [{"kind": "email|po|portal|system", '
    '"ref": "<reference number, or portal-login / portal-layout>"}]}'
)


def _stamp(moment: datetime) -> str:
    return f"{moment:%Y-%m-%d %H:%M} UTC"


def system_prompt() -> str:
    """Neutral system prompt for both arms: replaces Claude Code's default one, whose real date
    ("Today's date is ...") would contradict the simulated now (D35)."""
    return (
        "You are a careful assistant. Use only the tools you are given. "
        f"The current date and time is {_stamp(NOW)}."
    )


def _shared(tools: str) -> str:
    return f"""You are an operations assistant for a wholesale supplier. It is now {_stamp(NOW)}. \
You last looked at the business at {_stamp(LAST_LOOK)}.

List everything that needs attention since your last look. An item needs attention if, and only \
if, it meets one of these four rules:

1. Mail from a customer or a supplier that asks for action or reports a problem, received after \
your last look.
2. A purchase order (PO) that has been cancelled, or whose ETA has moved later by more than \
{ETA_SLIP_DAYS} days, since your last look.
3. A portal order that has been cancelled or put on hold since your last look.
4. Any problem that stops you from seeing a source now: the portal login has expired, or the \
portal layout has changed.

{tools}

You cannot ask questions; use your tools to find the answer. When you are done, you may write a \
short summary, then finish with a fenced JSON block of exactly this shape and nothing after it:

```json
{ANSWER_TEMPLATE}
```

kind is email, po, portal or system. ref is the item's reference number: the reference number \
in the email's subject, the PO number, or the portal order number; for rule 4 it is portal-login \
or portal-layout. List each item once.
"""


def build_prompt(arm: str) -> str:
    """The prompt for arm ``"A"`` (raw tools) or ``"B"`` (Since tools)."""
    try:
        tools = TOOLS[arm]
    except KeyError:
        raise ValueError(f"unknown arm {arm!r}; arms are {', '.join(ARMS)}") from None
    return _shared(tools)
