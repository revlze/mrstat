from openai import AsyncOpenAI

from .gemini import InlineImage


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


async def vision_chat_completion(
    *,
    api_key: str,
    base_url: str,
    model: str,
    messages: list[dict],
    images: list[InlineImage] | None = None,
    timeout: float = 60.0,
) -> str:
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=_chat_base_url(base_url),
        timeout=timeout,
    )
    response = await client.chat.completions.create(
        model=model,
        messages=_with_images(messages, images),
    )
    return response.choices[0].message.content or ""


def _with_images(messages: list[dict], images: list[InlineImage] | None) -> list[dict]:
    if not images:
        return messages

    prepared = [dict(message) for message in messages]
    last = prepared[-1]
    content = [{"type": "text", "text": str(last["content"])}]
    for image in images:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{image.mime_type};base64,{image.as_base64()}"},
            }
        )
    last["content"] = content
    return prepared


def _chat_base_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base[: -len("/chat/completions")]
    if base.endswith("/v1"):
        return base
    return f"{base}/v1"
