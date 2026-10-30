"""What the LLM calls of one step used. Apart from ``client.py`` so that the result models can
use it without importing the OpenAI SDK (the parents of the worker pools import them)."""

from pydantic import BaseModel


class LlmUsage(BaseModel):
    """What the LLM calls of one step used: the model, the calls made (a repair call counts)
    and the tokens the endpoint reported."""

    model: str
    calls: int
    prompt_tokens: int
    completion_tokens: int

    def plus(self, other: "LlmUsage") -> "LlmUsage":
        """The usage of both steps: their calls and tokens added, this step's model."""
        return LlmUsage(
            model=self.model,
            calls=self.calls + other.calls,
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
        )
