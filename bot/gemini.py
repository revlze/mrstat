import asyncio
import logging

from google import genai
from google.genai.errors import ServerError
from google.genai.types import GenerateContentConfig, GoogleSearch, Tool

logger = logging.getLogger(__name__)

_RETRY_DELAYS = [15, 30, 60]  # seconds between attempts
_RETRYABLE_STATUSES = ("500", "503", "504", "429")


async def chat_completion(
    *,
    api_key: str,
    model: str,
    messages: list[dict],
    response_format: dict | None = None,
    grounding: bool = False,
) -> str:
    client = genai.Client(api_key=api_key)

    system_instruction = None
    contents = []
    for msg in messages:
        if msg["role"] == "system":
            system_instruction = msg["content"]
        else:
            contents.append(msg["content"])

    config = GenerateContentConfig(
        system_instruction=system_instruction,
        response_mime_type=(
            None
            if grounding
            else (
                "application/json"
                if response_format and response_format.get("type") == "json_object"
                else None
            )
        ),
        tools=[Tool(google_search=GoogleSearch())] if grounding else None,
    )

    last_exc: Exception | None = None
    for attempt, delay in enumerate([0] + _RETRY_DELAYS, 1):
        if delay:
            logger.warning(
                "Gemini transient error, retry %d/%d in %ds: %s",
                attempt, len(_RETRY_DELAYS) + 1, delay, last_exc,
            )
            await asyncio.sleep(delay)
        try:
            response = await client.aio.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
            return response.text
        except ServerError as exc:
            if not any(code in str(exc) for code in _RETRYABLE_STATUSES):
                raise
            last_exc = exc
    raise last_exc
