"""Pure mapping from the judge's answers to a code_puppy model name."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import COMPLEXITY_LEVEL_COUNT, RouterConfig
from .judge_client import JudgeAnswers, JudgeResult

# Why a decision landed where it did.
REASON_JUDGED = "judged"  # the judge scored the request and the policy picked a tier
REASON_LOW_CONFIDENCE = "low_confidence"  # confidence under the threshold: fallback
REASON_ROUNDED_UP = "rounded_up"  # unsure between adjacent levels: took the higher one
REASON_JUDGE_ERROR = "judge_error"  # judge unreachable or malformed: fallback
REASON_NO_API_KEY = "no_api_key"  # hosted judge needs a key and none is set: fallback
REASON_EMPTY_PROMPT = "empty_prompt"  # nothing to judge: fallback
REASON_UNKNOWN_MODEL = (
    "unknown_model"  # the chosen tier is not a registered model: fallback
)


@dataclass
class Decision:
    model: str
    reason: str
    notes: List[str] = field(default_factory=list)
    level: Optional[int] = None
    confidence: Optional[float] = None
    risky: Optional[float] = None
    probabilities: Dict[str, float] = field(default_factory=dict)
    judge_model: Optional[str] = None
    judge_latency_ms: Optional[int] = None
    judge_usage: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "model": self.model,
            "reason": self.reason,
            "notes": list(self.notes),
        }
        for key in (
            "level",
            "confidence",
            "risky",
            "judge_model",
            "judge_latency_ms",
            "error",
        ):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        if self.probabilities:
            data["probabilities"] = dict(self.probabilities)
        if self.judge_usage:
            data["judge_usage"] = dict(self.judge_usage)
        return data

    def summary(self) -> str:
        bits = [f"{self.model} ({self.reason}"]
        if self.level is not None:
            bits.append(f", level {self.level}")
        if self.confidence is not None:
            bits.append(f", confidence {self.confidence:.2f}")
        if self.risky is not None:
            bits.append(f", risky {self.risky:.2f}")
        return "".join(bits) + ")"


# In next_level mode, this much probability on the expert level still sends the
# turn to the fallback: the doubt is about whether the task is hard at all.
EXPERT_WEIGHT = 0.2


def likely_levels(answers: JudgeAnswers, level: int) -> List[int]:
    """The two most probable levels, highest probability first."""
    probs = {}
    for key, value in answers.complexity_probabilities.items():
        try:
            probs[int(key)] = float(value)
        except (TypeError, ValueError):
            continue
    if not probs:
        return [level, min(level + 1, COMPLEXITY_LEVEL_COUNT - 1)]
    ranked = sorted(probs, key=lambda k: probs[k], reverse=True)
    return [clamp_level(k) for k in ranked[:2]]


def clamp_level(score: float) -> int:
    return max(0, min(COMPLEXITY_LEVEL_COUNT - 1, int(round(score))))


def apply_policy(answers: JudgeAnswers, cfg: RouterConfig) -> Decision:
    notes: List[str] = []
    level = clamp_level(answers.complexity_score)
    tier = cfg.complexity_to_tier[level]
    notes.append(
        f"complexity {answers.complexity_score:.2f} -> level {level} -> {tier}"
    )

    floor = cfg.risk_floor
    if (
        floor
        and answers.risky >= floor.threshold
        and cfg.tier_index(floor.min_tier) > cfg.tier_index(tier)
    ):
        notes.append(
            f"risky {answers.risky:.2f} >= {floor.threshold} -> {floor.min_tier}"
        )
        tier = floor.min_tier

    reason = REASON_JUDGED
    if answers.complexity_confidence < cfg.confidence_threshold:
        unsure = f"confidence {answers.complexity_confidence:.2f} < {cfg.confidence_threshold}"
        expert_p = float(
            answers.complexity_probabilities.get(str(COMPLEXITY_LEVEL_COUNT - 1), 0.0)
        )
        if cfg.low_confidence == "fallback" or expert_p >= EXPERT_WEIGHT:
            why = (
                f"expert p={expert_p:.2f} >= {EXPERT_WEIGHT}"
                if cfg.low_confidence != "fallback"
                else "low_confidence=fallback"
            )
            notes.append(f"{unsure}, {why} -> fallback {cfg.fallback_model}")
            tier = cfg.fallback_model
            reason = REASON_LOW_CONFIDENCE
        else:
            candidates = likely_levels(answers, level)
            higher = max(candidates)
            rounded = cfg.complexity_to_tier[higher]
            if cfg.tier_index(rounded) > cfg.tier_index(tier):
                tier = rounded
            notes.append(
                f"{unsure}, likely levels {sorted(candidates)} -> level {higher} -> {tier}"
            )
            reason = REASON_ROUNDED_UP

    return Decision(
        model=tier,
        reason=reason,
        notes=notes,
        level=level,
        confidence=answers.complexity_confidence,
        risky=answers.risky,
        probabilities=dict(answers.complexity_probabilities),
    )


def decision_from_result(result: JudgeResult, cfg: RouterConfig) -> Decision:
    decision = apply_policy(result.answers, cfg)
    decision.judge_model = result.model
    decision.judge_latency_ms = result.latency_ms
    decision.judge_usage = dict(result.usage)
    return decision


def fallback_decision(
    cfg: RouterConfig, reason: str, note: str, error: Optional[str] = None
) -> Decision:
    return Decision(model=cfg.fallback_model, reason=reason, notes=[note], error=error)
