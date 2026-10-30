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
