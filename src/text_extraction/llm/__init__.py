"""The LLM side of the service: the OpenAI-compatible client (``client.py``) and what needs no
SDK (``basics.py``: ``LlmError``, ``truncate_input``). Nothing is imported here, so
``import text_extraction.llm`` stays free of the OpenAI SDK until a client is needed."""
