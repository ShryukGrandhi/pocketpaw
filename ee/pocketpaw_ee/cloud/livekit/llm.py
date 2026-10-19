"""Raw-HTTP LLM calls shared by the call bot and Ask Paw.

``call_llm_text`` is the one-shot request the meeting-notes bot uses for its
summaries; ``stream_text`` streams text deltas for Ask Paw's spoken answers;
``split_sentences`` turns a delta stream into whole sentences so each one can
be voiced as soon as it is complete.

Providers are tried in a fixed order, DeepSeek -> Anthropic -> OpenAI, keyed on
the raw ``DEEPSEEK_API_KEY`` / ``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY`` env
vars. ``stream_text`` only falls through to the next provider when the current
one fails before yielding anything: once a delta has gone out it may already
have been spoken, so a mid-stream failure is raised instead of restarting the
answer on another provider.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

_DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_OPENAI_URL = "https://api.openai.com/v1/chat/completions"

_STREAM_PROVIDERS: tuple[tuple[str, str, str], ...] = (
    ("deepseek", "DEEPSEEK_API_KEY", "deepseek-chat"),
    ("anthropic", "ANTHROPIC_API_KEY", "claude-sonnet-4-20250514"),
    ("openai", "OPENAI_API_KEY", "gpt-4o-mini"),
)

_SENTENCE_END = re.compile(r"(?<=[.!?])[\"')\]]*\s+")


class NoLLMConfigured(RuntimeError):
    """No provider key is set, or every provider failed before streaming."""


async def call_llm_text(
    provider: str,
    api_key: str,
    prompt: str,
    model: str,
    max_tokens: int,
    timeout: int,
) -> str:
    """Send one user prompt to ``provider`` and return the response text.

    Supports 'deepseek', 'anthropic', 'openai'. Raises on HTTP failure.
    """
    import httpx

    if provider in ("deepseek", "openai"):
        url = _DEEPSEEK_URL if provider == "deepseek" else _OPENAI_URL
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("choices", [{}])[0].get("message", {}).get("content", "")

    if provider == "anthropic":
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                _ANTHROPIC_URL,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("content", [{}])[0].get("text", "")

    raise ValueError(f"Unknown LLM provider: {provider}")


async def _sse_data(resp) -> AsyncIterator[str]:
    async for line in resp.aiter_lines():
        if line.startswith("data:"):
            yield line[5:].strip()


async def _stream_provider(
    provider: str,
    api_key: str,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    timeout: float,
) -> AsyncIterator[str]:
    import httpx

    if provider == "anthropic":
        url = _ANTHROPIC_URL
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": model,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "stream": True,
        }
    else:
        url = _DEEPSEEK_URL if provider == "deepseek" else _OPENAI_URL
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "stream": True,
        }

    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, headers=headers, json=body) as resp:
            resp.raise_for_status()
            async for data in _sse_data(resp):
                if not data or data == "[DONE]":
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if provider == "anthropic":
                    if event.get("type") == "content_block_delta":
                        text = (event.get("delta") or {}).get("text") or ""
                    else:
                        text = ""
                else:
                    choices = event.get("choices") or [{}]
                    text = (choices[0].get("delta") or {}).get("content") or ""
                if text:
                    yield text


async def stream_text(
    system: str,
    user: str,
    *,
    max_tokens: int = 400,
    timeout: float = 30.0,
) -> AsyncIterator[str]:
    """Stream response text deltas from the first provider that answers."""
    attempted = False
    for provider, env_key, model in _STREAM_PROVIDERS:
        api_key = os.environ.get(env_key, "")
        if not api_key:
            continue
        attempted = True
        started = False
        try:
            async for delta in _stream_provider(
                provider, api_key, model, system, user, max_tokens, timeout
            ):
                started = True
                yield delta
            if started:
                return
            logger.warning("Ask Paw: %s streamed an empty answer, trying next provider", provider)
        except Exception as exc:
            if started:
                raise
            logger.warning("Ask Paw: %s stream failed before output: %s", provider, exc)
    if attempted:
        raise NoLLMConfigured("Every configured LLM provider failed")
    raise NoLLMConfigured("No LLM provider key is configured")


async def split_sentences(deltas: AsyncIterator[str]) -> AsyncIterator[str]:
    """Regroup a text-delta stream into complete sentences; flush the rest at the end."""
    buffer = ""
    async for delta in deltas:
        buffer += delta
        while True:
            match = _SENTENCE_END.search(buffer)
            if match is None:
                break
            sentence = buffer[: match.end()].strip()
            buffer = buffer[match.end() :]
            if sentence:
                yield sentence
    tail = buffer.strip()
    if tail:
        yield tail
