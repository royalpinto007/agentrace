"""Failure patterns for subagent output.

Every check here comes from a failure that actually happened, not from imagination. They were
collected while running ~100 research subagents over two weeks, and each one cost real time or
nearly produced a wrong action.

The thesis: directing agents is the easy half. The hard half is knowing which of their answers to
trust. A model is good at producing candidates and bad at knowing what counts as proof, so the job
is designing the verification.

These are heuristics over text, so they are hints, not verdicts. Each finding says what to check
rather than asserting a fact. A checker that cries wolf gets ignored, which is worse than no
checker at all, so severity is deliberately conservative.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from .parse import AgentRun


@dataclass
class Finding:
    check: str
    severity: str  # "high" | "medium" | "low"
    message: str
    evidence: str = ""


# --------------------------------------------------------------------------- checks


def check_empty_result(run: AgentRun) -> list[Finding]:
    """An agent that returned nothing.

    Cheap to detect, easy to miss when you are reading a wall of output, and it silently means the
    work did not happen.
    """
    if run.is_error:
        return [Finding("error", "high", "Subagent returned an error", run.result[:200])]
    if run.result_chars == 0:
        return [Finding("empty_result", "high", "Subagent returned nothing")]
    if run.result_chars < 80:
        return [
            Finding(
                "thin_result",
                "medium",
                f"Result is only {run.result_chars} chars, likely incomplete",
                run.result[:200],
            )
        ]
    return []


def check_refused_or_gave_up(run: AgentRun) -> list[Finding]:
    """The agent politely did nothing.

    "I was unable to find..." reads as an answer if you skim. It is not one.
    """
    patterns = [
        r"\bI (?:was )?(?:un|not )able to\b",
        r"\bI (?:could|couldn'?t|cannot|can't) (?:find|access|complete|determine)\b",
        r"\bno (?:results|data|information) (?:were |was )?found\b",
        r"\bI don'?t have (?:access|enough)\b",
    ]
    head = run.result[:1500]
    for p in patterns:
        m = re.search(p, head, re.I)
        if m:
            return [
                Finding(
                    "gave_up",
                    "medium",
                    "Subagent reported it could not do the task",
                    _context(head, m.start()),
                )
            ]
    return []


def check_unverified_claim(run: AgentRun) -> list[Finding]:
    """Hedged language presented as a finding.

    This is the one that bit hardest. An agent said a company "appears to be" hiring, and that
    became a fact by the time it reached a decision. Hedges are honest, but they must survive into
    the next step rather than being flattened.
    """
    hedges = [
        r"\b(?:appears|seems|seemed) to be\b",
        r"\blikely (?:the|a|that)\b",
        r"\bprobably\b",
        r"\bI (?:assume|believe|think) (?:that )?\b",
        r"\bcould not (?:independently )?verify\b",
        r"\bunverified\b",
    ]
    hits = []
    for p in hedges:
        for m in re.finditer(p, run.result, re.I):
            hits.append(_context(run.result, m.start()))
            break
    if hits:
        return [
            Finding(
                "hedged_claim",
                "low",
                f"{len(hits)} hedged claim(s). Fine if the hedge survives downstream, a problem if it gets flattened into fact.",
                hits[0],
            )
        ]
    return []


def check_absence_as_evidence(run: AgentRun) -> list[Finding]:
    """Treating "I found nothing" as "there is nothing".

    The real case: an agent concluded a company was not hiring because an API returned an empty
    list. That API returns empty with HTTP 200 for accounts that do not exist. Absence of data is
    not evidence of absence, and the difference is the whole finding.
    """
    patterns = [
        r"\bno (?:open )?(?:roles|jobs|positions|openings)\b",
        r"\breturned (?:an )?empty\b",
        r"\bnothing (?:was )?found\b",
        r"\bboard is empty\b",
    ]
    for p in patterns:
        m = re.search(p, run.result, re.I)
        if m:
            return [
                Finding(
                    "absence_as_evidence",
                    "medium",
                    "Concludes something does not exist from a negative result. Check the negative is real and not a null signal (a 200 with an empty body, a 404 on a valid resource, a JS-rendered page).",
                    _context(run.result, m.start()),
                )
            ]
    return []


def check_url_without_verification(run: AgentRun) -> list[Finding]:
    """Lots of links, no sign anything was opened.

    An agent that lists twenty URLs it never fetched is doing autocomplete, not research.
    """
    urls = re.findall(r"https?://[^\s)\]<>\"']+", run.result)
    if len(urls) < 5:
        return []
    verified = re.search(r"\b(?:verified|confirmed|checked|fetched|HTTP 200|status 200)\b", run.result, re.I)
    if not verified:
        return [
            Finding(
                "unverified_urls",
                "medium",
                f"{len(urls)} URLs cited with no mention of verification. Did the agent open them, or pattern-match them?",
                urls[0],
            )
        ]
    return []


def check_prompt_hygiene(run: AgentRun) -> list[Finding]:
    """The failure that is your fault, not the agent's.

    A vague prompt produces a vague answer, and then you blame the model. If the task has no
    definition of done, the agent cannot know when it is finished, and neither can you.
    """
    out: list[Finding] = []

    # Any signal that the caller said what "done" looks like: a destination, a shape, or a verb
    # that implies one. Deliberately generous, because a false "you forgot the output contract"
    # on a prompt that has one is exactly the noise that gets a linter switched off.
    has_output_spec = re.search(
        r"\b(?:output|outputs|return|returns|format|formatted|respond|reply|write|writing|report|"
        r"summar[iy]|list|table|json|csv|markdown|schema|fields|columns|deliver)\b",
        run.prompt,
        re.I,
    )

    # Length alone is not the defect. "Run the suite and report every failing test as node ids
    # with its assertion message" is 113 chars and perfectly verifiable; flagging it taught nobody
    # anything and spent the reader's attention. What makes a prompt thin is being short *and*
    # never saying what done looks like, so both signals have to fire.
    if run.prompt_chars < 200 and not has_output_spec:
        out.append(
            Finding(
                "thin_prompt",
                "low",
                f"Prompt is {run.prompt_chars} chars and never says what the output should be. You cannot verify an answer to a question you did not really ask.",
            )
        )

    if run.prompt_chars >= 200 and not has_output_spec:
        out.append(
            Finding(
                "no_output_contract",
                "low",
                "Prompt does not specify an output shape, so the result is whatever the agent felt like returning. Hard to verify, hard to parse.",
            )
        )
    return out


def check_runaway(run: AgentRun, slow_s: float = 900.0) -> list[Finding]:
    """Very long runs.

    Not wrong by itself, but a subagent running for 25 minutes is usually looping, retrying, or
    doing something you did not intend to pay for.
    """
    d = run.duration_s
    if d is not None and d > slow_s:
        return [
            Finding(
                "slow_run",
                "low",
                f"Ran for {d/60:.1f} min. Worth checking it was not looping or retrying.",
            )
        ]
    return []


CHECKS: list[Callable[[AgentRun], list[Finding]]] = [
    check_empty_result,
    check_refused_or_gave_up,
    check_unverified_claim,
    check_absence_as_evidence,
    check_url_without_verification,
    check_prompt_hygiene,
    check_runaway,
]


def analyse(run: AgentRun) -> list[Finding]:
    findings: list[Finding] = []
    for check in CHECKS:
        findings.extend(check(run))
    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: order.get(f.severity, 9))
    return findings


def _context(text: str, pos: int, width: int = 70) -> str:
    start = max(0, pos - width // 2)
    snippet = text[start : start + width].replace("\n", " ").strip()
    return f"...{snippet}..." if start > 0 else f"{snippet}..."
