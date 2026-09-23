"""Minimal synchronous client for TypeSafe's systemone endpoint.

Sync on purpose: the ``model_select`` hook runs synchronously inside the
agent's event loop, and the call is a single short POST. Works with Kev
(the open-source clone) by changing ``base_url``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import httpx

from .questions import ROUTING_QUESTIONS


class JudgeError(RuntimeError):
    """The judge was unreachable, rejected the request, or returned an unusable answer."""


@dataclass
class JudgeAnswers:
    complexity_score: float
    complexity_confidence: float
    complexity_probabilities: Dict[str, float]
    risky: float


@dataclass
class JudgeResult:
    answers: JudgeAnswers
    model: str
    latency_ms: int
    usage: Dict[str, Any] = field(default_factory=dict)


def _number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JudgeError(f"judge answer is missing {what}")
    return float(value)


def parse_answers(payload: Dict[str, Any]) -> JudgeAnswers:
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        raise JudgeError("judge response has no 'answers'")
    complexity = answers.get("complexity") or {}
    risky = answers.get("risky") or {}
    if not isinstance(complexity, dict) or not isinstance(risky, dict):
        raise JudgeError("judge response has malformed answers")
    probabilities = complexity.get("probabilities") or {}
    return JudgeAnswers(
        complexity_score=_number(complexity.get("score"), "complexity.score"),
        complexity_confidence=_number(
            complexity.get("confidence"), "complexity.confidence"
        ),
        complexity_probabilities={str(k): float(v) for k, v in probabilities.items()}
        if isinstance(probabilities, dict)
        else {},
        risky=_number(risky.get("noul"), "risky.noul"),
    )


def ask_judge(
    state: Dict[str, Any],
    *,
    api_key: Optional[str],
    base_url: str,
    model: str,
    timeout_s: float,
    transport: Optional[httpx.BaseTransport] = None,
) -> JudgeResult:
    """One systemone call with the two routing questions."""
    import time

    body = {"state": state, "model": model, "questions": ROUTING_QUESTIONS}
    started = time.monotonic()
    try:
        with httpx.Client(
            base_url=base_url,
            timeout=timeout_s,
            transport=transport,
            headers={
                **({"Authorization": f"Bearer {api_key}"} if api_key else {}),
                "Content-Type": "application/json",
                "User-Agent": "code-puppy-model-router",
            },
        ) as client:
            response = client.post("/v1/systemone", json=body)
    except httpx.HTTPError as exc:
        raise JudgeError(
            f"judge request failed: {exc.__class__.__name__}: {exc}"
        ) from exc
    latency_ms = int((time.monotonic() - started) * 1000)

    if response.status_code >= 400:
        detail = response.text[:300].replace("\n", " ")
        raise JudgeError(f"judge returned HTTP {response.status_code}: {detail}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise JudgeError("judge returned a non-JSON body") from exc
    if not isinstance(payload, dict):
        raise JudgeError("judge returned an unexpected body")

    usage = payload.get("usage")
    return JudgeResult(
        answers=parse_answers(payload),
        model=str(payload.get("model") or model),
        latency_ms=latency_ms,
        usage=usage if isinstance(usage, dict) else {},
    )
