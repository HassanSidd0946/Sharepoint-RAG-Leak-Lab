"""The language model of the agent: a deterministic stub for every benchmark number.

StubLLM answers by QUOTING the text it is given, verbatim and in full. It is the worst-case leaker:
whatever reaches it reaches the user, so any leak in S6 is a leak of the surrounding system (the
cache, the memory, the retrieval) and never a model hallucination, and a result does not depend on
a model that could change. The answer is a pure function of (question, contexts).

RealLLM calls an Anthropic model for exploratory use only (`python -m leaklab.agent ask --real-llm`).
Nothing in the S6 benchmark may use it: the S6 driver has no such option and refuses one. UNVERIFIED:
it has not been exercised against the live API in this project (no key was used).
"""

import os
from collections.abc import Sequence
from typing import Protocol

from leaklab.agent.types import Context

NO_CONTEXT = "I could not find any documents you may read for this question."


class LLM(Protocol):
    name: str

    def answer(self, question: str, contexts: Sequence[Context]) -> str: ...


class StubLLM:
    name = "stub"

    def answer(self, question: str, contexts: Sequence[Context]) -> str:
        if not contexts:
            return NO_CONTEXT
        quoted = "\n---\n".join(c.text for c in contexts)
        return f"Here is what the documents say:\n{quoted}"


class RealLLM:
    """An Anthropic model behind the same interface (exploratory only, see the module doc)."""

    def __init__(self, model: str = "claude-sonnet-5-5") -> None:
        try:
            import anthropic
        except ImportError as exc:
            raise RuntimeError(
                "the real LLM needs the `anthropic` package (uv add anthropic) and ANTHROPIC_API_KEY"
            ) from exc
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        self.name = f"real:{model}"
        self._model = model
        self._client = anthropic.Anthropic()

    def answer(self, question: str, contexts: Sequence[Context]) -> str:
        if not contexts:
            return NO_CONTEXT
        sources = "\n---\n".join(c.text for c in contexts)
        message = self._client.messages.create(
            model=self._model,
            max_tokens=600,
            messages=[
                {
                    "role": "user",
                    "content": f"Answer using only these documents.\n\n{sources}\n\nQuestion: {question}",
                }
            ],
        )
        return "".join(getattr(block, "text", "") for block in message.content)
