import httpx


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


async def chat_completion(
    *,
    api_key: str,
    model: str,
    messages: list[dict],
    response_format: dict | None = None,
    timeout: float = 60.0,
) -> dict:
    payload: dict = {"model": model, "messages": messages, "reasoning": {"enabled": True}}
    if response_format is not None:
        payload["response_format"] = response_format

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(OPENROUTER_URL, json=payload, headers=headers)
        response.raise_for_status()
        return response.json()
