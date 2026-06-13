from openai import AsyncOpenAI


async def chat_completion(
    *,
    api_key: str,
    base_url: str,
    model: str,
    messages: list[dict],
    response_format: dict | None = None,
    timeout: float = 60.0,
) -> str:
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=_chat_base_url(base_url),
        timeout=timeout,
    )
    response = await client.chat.completions.create(
        model=model,
        messages=messages,
        response_format=response_format,
    )
    return response.choices[0].message.content or ""


def _chat_base_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base[: -len("/chat/completions")]
    if base.endswith("/v1"):
        return base
    return f"{base}/v1"
