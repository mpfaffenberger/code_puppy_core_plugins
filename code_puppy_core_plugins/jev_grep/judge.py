"""Relevance judgments from Jev, TypeSafe's decision model, via Pydantic AI.

Relevance is scored as the product of two independent judgments -- the snippet
acts on the requested *entity*, and it performs the requested *operation* -- so
mentioning a concept is not enough; the code must do the thing. A bounded
``float`` field on a decision model is answered with the raw probability of
yes, so there is no confidence-margin math to undo.

Every snippet is judged in its own run (its own Jev state), so neighbouring
snippets never lend each other evidence.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model

from .chunks import Chunk

DEFAULT_CONCURRENCY = 8
REQUEST_TIMEOUT_SECONDS = 30


class Relevance(BaseModel):
    """Decide whether this source code is what a developer searching the codebase wants to find."""

    entity: float = Field(
        ge=0,
        le=1,
        description=(
            "Does this code act on the thing the search is about "
            "(the data, object, resource or concept it names)?"
        ),
    )
    operation: float = Field(
        ge=0,
        le=1,
        description=(
            "Does this code itself perform the action or behaviour the search "
            "describes, rather than only mentioning it, importing it, or calling "
            "something with a similar name?"
        ),
    )

    @property
    def score(self) -> float:
        return self.entity * self.operation


def build_jev_model(model_name: str, api_key: str) -> Model:
    """A TypeSafe Jev model. Imported lazily: the SDK is an optional extra."""
    from pydantic_ai.models.typesafe import TypeSafeModel
    from pydantic_ai.providers.typesafe import TypeSafeProvider

    return TypeSafeModel(model_name, provider=TypeSafeProvider(api_key=api_key))


def _state(chunk: Chunk) -> str:
    """What Jev judges: where the snippet lives, then the snippet itself."""
    where = f"{chunk.path}:{chunk.line}-{chunk.end_line}"
    header = f"{where} ({chunk.symbol})" if chunk.symbol else where
    return f"{header}\n\n{chunk.text}"


def build_agent(model: Model, query: str) -> Agent[None, Relevance]:
    return Agent(
        model,
        output_type=Relevance,
        instructions=(
            "The text is a snippet of source code from a repository. "
            f"A developer is searching the codebase for: {query}"
        ),
        model_settings={"timeout": REQUEST_TIMEOUT_SECONDS},
    )


async def judge(
    model: Model,
    query: str,
    chunks: Sequence[Chunk],
    concurrency: int = DEFAULT_CONCURRENCY,
) -> list[float]:
    """Score each chunk in [0, 1], order preserved.

    Any failed judgment fails the whole search (siblings are cancelled):
    a partial scan reported as a complete one is worse than an error.
    """
    agent = build_agent(model, query)
    gate = asyncio.Semaphore(concurrency)

    async def one(chunk: Chunk) -> float:
        async with gate:
            result = await agent.run(_state(chunk))
        return result.output.score

    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(one(chunk)) for chunk in chunks]
    return [task.result() for task in tasks]
