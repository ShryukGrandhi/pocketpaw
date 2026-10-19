"""Ask Paw: answer a question asked during a LiveKit call, out loud.

Runs in the API process. The call bot (``agent.py``, a subprocess) owns the
room; this module talks to it only through Redis:

- reads ``livekit:transcript:{group_id}`` (live segments the bot mirrors) and
  ``livekit:progressive:{group_id}`` (5-minute summaries) without consuming them;
- PUBLISHes commands on ``livekit:bot:cmd:{group_id}`` (``say``, ``say_end``,
  ``stop``, ``listen_start``, ``listen_end``);
- BLPOPs a spoken question from ``livekit:ask:q:{ask_id}`` after ``listen_end``;
- holds ``livekit:ask:lock:{group_id}`` (SET NX EX, value = ask_id) so one call
  answers one question at a time. Only the holder releases it, except ``stop``.

Grounding is the live call, the workspace knowledge base and workspace-shared
memory facts. The room has several members, so nothing member-private is read:
KB uses only the ``workspace:{id}`` scope (never ``user:``), and memory facts
attributed to one member are skipped. Every state change is emitted as
``call.assistant_state`` to the group's members.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable
from typing import Any

from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import CallAssistantState
from pocketpaw_ee.cloud.livekit.agent import (
    _REDIS_PROGRESSIVE_KEY,
    REDIS_ASK_QUESTION_KEY,
    REDIS_BOT_CMD_CHANNEL,
    REDIS_TRANSCRIPT_KEY,
)
from pocketpaw_ee.cloud.livekit.llm import split_sentences, stream_text
from pocketpaw_ee.cloud.livekit.prompts import ASK_PAW_PROMPT

logger = logging.getLogger(__name__)

ASK_LOCK_KEY = "livekit:ask:lock:{group_id}"
ASK_LOCK_TTL_SECONDS = 90
ASK_RATE_KEY = "livekit:ask:rate:{user_id}"
ASK_RATE_LIMIT = 10
ASK_RATE_WINDOW_SECONDS = 60

_GATHER_TIMEOUT_SECONDS = 1.5
_TRANSCRIPT_TAIL_SEGMENTS = 2000
_KB_MAX_CHARS = 4000
_MEMORY_FETCH_LIMIT = 200
_MEMORY_TOP_K = 8
_SHARED_MEMORY_USER_IDS = (None, "", "default")

DIDNT_CATCH_THAT = "Sorry, I didn't catch that."
_APOLOGY = "Sorry, I couldn't answer that right now."

_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "the and for are was were what when where which who why how this that with from "
    "have has had you your our we they them did does can could would should about "
    "into there their then than just paw".split()
)

_ask_tasks: dict[str, asyncio.Task] = {}


class _AskStopped(Exception):
    """The ask lock moved on (``stop`` from any web process, or it expired); stop speaking."""


def _redis() -> Any:
    from pocketpaw_ee.cloud._core.redis_client import get_redis

    return get_redis()


def _blocking_redis() -> Any:
    from pocketpaw_ee.cloud._core.redis_client import get_blocking_redis

    return get_blocking_redis()


def new_ask_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Lock, rate limit, bot commands
# ---------------------------------------------------------------------------


async def acquire_ask_lock(group_id: str, ask_id: str) -> bool:
    key = ASK_LOCK_KEY.format(group_id=group_id)
    return bool(await _redis().set(key, ask_id, nx=True, ex=ASK_LOCK_TTL_SECONDS))


async def ask_lock_holder(group_id: str) -> str | None:
    return await _redis().get(ASK_LOCK_KEY.format(group_id=group_id))


async def release_ask_lock(group_id: str, ask_id: str) -> bool:
    """Release the lock only if ``ask_id`` still holds it."""
    key = ASK_LOCK_KEY.format(group_id=group_id)
    return bool(await _redis().eval(_RELEASE_LUA, 1, key, ask_id))


async def force_release_ask_lock(group_id: str) -> None:
    await _redis().delete(ASK_LOCK_KEY.format(group_id=group_id))


async def allow_ask(user_id: str) -> bool:
    """Fixed-window per-user rate limit: ``ASK_RATE_LIMIT`` asks a minute."""
    redis = _redis()
    key = ASK_RATE_KEY.format(user_id=user_id)
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, ASK_RATE_WINDOW_SECONDS)
    return count <= ASK_RATE_LIMIT


async def publish_bot_command(group_id: str, command: dict[str, Any]) -> None:
    channel = REDIS_BOT_CMD_CHANNEL.format(group_id=group_id)
    await _redis().publish(channel, json.dumps(command))


async def emit_assistant_state(
    *,
    group_id: str,
    state: str,
    ask_id: str | None,
    asked_by: str | None,
    asked_by_name: str | None,
    question: str | None = None,
    answer: str | None = None,
    error: str | None = None,
) -> None:
    try:
        await emit(
            CallAssistantState(
                data={
                    "group_id": group_id,
                    "state": state,
                    "ask_id": ask_id,
                    "asked_by": asked_by,
                    "asked_by_name": asked_by_name,
                    "question": question,
                    "answer": answer,
                    "error": error,
                }
            )
        )
    except Exception:
        logger.warning("Ask Paw: failed to emit %s state for group %s", state, group_id)


def start_ask_task(group_id: str, coro: Awaitable[Any]) -> asyncio.Task:
    """Run an ask in the background, keeping a reference so ``stop`` can cancel it."""
    task = asyncio.ensure_future(coro)
    _ask_tasks[group_id] = task

    def _done(t: asyncio.Task) -> None:
        if _ask_tasks.get(group_id) is t:
            _ask_tasks.pop(group_id, None)

    task.add_done_callback(_done)
    return task


async def stop_ask(group_id: str) -> None:
    """Silence Paw now: cancel the running answer, tell the bot to stop, free the lock."""
    task = _ask_tasks.pop(group_id, None)
    if task is not None and not task.done():
        task.cancel()
    try:
        await publish_bot_command(group_id, {"type": "stop", "ask_id": None})
    finally:
        await force_release_ask_lock(group_id)


# ---------------------------------------------------------------------------
# Context gathering
# ---------------------------------------------------------------------------


async def read_live_transcript(group_id: str, max_chars: int = 12000) -> str:
    """Progressive summaries plus the most recent transcript lines within ``max_chars``.

    Read-only: the progressive key is consumed by the bot at call end, never here.
    """
    redis = _redis()
    raw_segments = await redis.lrange(
        REDIS_TRANSCRIPT_KEY.format(group_id=group_id), -_TRANSCRIPT_TAIL_SEGMENTS, -1
    )
    raw_summaries = await redis.lrange(_REDIS_PROGRESSIVE_KEY.format(group_id=group_id), 0, -1)

    summaries: list[str] = []
    for raw in raw_summaries or []:
        try:
            text = str(json.loads(raw).get("segment_summary") or "").strip()
        except (ValueError, AttributeError):
            continue
        if text:
            summaries.append(f"- {text}")

    lines: list[str] = []
    budget = max_chars
    for raw in reversed(raw_segments or []):
        try:
            seg = json.loads(raw)
        except ValueError:
            continue
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        line = f"{seg.get('speaker') or 'Someone'}: {text}"
        if len(line) + 1 > budget:
            break
        budget -= len(line) + 1
        lines.append(line)
    lines.reverse()

    parts: list[str] = []
    if summaries:
        parts.append("Earlier in the call (summaries):\n" + "\n".join(summaries))
    if lines:
        parts.append("Recent conversation:\n" + "\n".join(lines))
    return "\n\n".join(parts)


async def _kb_context(workspace_id: str, question: str) -> str:
    from pocketpaw_ee.cloud.agents.knowledge import KnowledgeService

    text = await KnowledgeService.search_context_for_scope(
        f"workspace:{workspace_id}", question, limit=3
    )
    return (text or "").strip()[:_KB_MAX_CHARS]


def _terms(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if len(w) > 2 and w not in _STOPWORDS}


async def _memory_facts(workspace_id: str, question: str) -> list[str]:
    from pocketpaw_ee.cloud.memory.mongo_store import MongoMemoryStore

    entries = await MongoMemoryStore().list_facts_in_workspace(
        workspace_id, limit=_MEMORY_FETCH_LIMIT
    )
    wanted = _terms(question)
    if not wanted:
        return []
    scored: list[tuple[int, str]] = []
    for entry in entries:
        if (entry.metadata or {}).get("user_id") not in _SHARED_MEMORY_USER_IDS:
            continue
        score = len(wanted & _terms(entry.content))
        if score:
            scored.append((score, entry.content.strip()))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [content for _, content in scored[:_MEMORY_TOP_K]]


async def _bounded(label: str, coro: Awaitable[Any], default: Any) -> Any:
    started = time.monotonic()
    try:
        return await asyncio.wait_for(coro, timeout=_GATHER_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.warning("Ask Paw: %s timed out after %.1fs", label, _GATHER_TIMEOUT_SECONDS)
    except Exception:
        logger.warning("Ask Paw: %s failed", label, exc_info=True)
    finally:
        logger.info("Ask Paw: %s took %.0fms", label, (time.monotonic() - started) * 1000)
    return default


def _who(group_id: str, ask_id: str, user_id: str, asked_by_name: str) -> dict[str, str]:
    return {
        "group_id": group_id,
        "ask_id": ask_id,
        "asked_by": user_id,
        "asked_by_name": asked_by_name,
    }


def _build_user_prompt(
    *, question: str, asked_by_name: str, call: str, kb: str, facts: list[str]
) -> str:
    sections = [
        f"<call-context>\n{call or '(nothing transcribed yet)'}\n</call-context>",
        f"<workspace-knowledge>\n{kb or '(none found)'}\n</workspace-knowledge>",
        "<workspace-memory>\n"
        + ("\n".join(f"- {f}" for f in facts) if facts else "(none found)")
        + "\n</workspace-memory>",
        f"{asked_by_name} asks: {question}",
    ]
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Answering
# ---------------------------------------------------------------------------


async def answer_call_question(
    *,
    group_id: str,
    workspace_id: str,
    user_id: str,
    asked_by_name: str,
    question: str,
    ask_id: str,
) -> str | None:
    """Think, speak the answer sentence by sentence, record it in chat, then go idle.

    The caller must already hold the ask lock for ``ask_id``; it is released here.
    """
    who = _who(group_id, ask_id, user_id, asked_by_name)
    started = time.monotonic()
    spoken: list[str] = []
    try:
        await emit_assistant_state(state="thinking", question=question, **who)

        call, kb, facts = await asyncio.gather(
            _bounded("live transcript", read_live_transcript(group_id), ""),
            _bounded("knowledge base", _kb_context(workspace_id, question), ""),
            _bounded("memory facts", _memory_facts(workspace_id, question), []),
        )
        gathered = time.monotonic()

        prompt = _build_user_prompt(
            question=question, asked_by_name=asked_by_name, call=call, kb=kb, facts=facts
        )
        first_sentence_at: float | None = None
        async for sentence in split_sentences(stream_text(ASK_PAW_PROMPT, prompt)):
            if await ask_lock_holder(group_id) != ask_id:
                raise _AskStopped
            if not spoken:
                first_sentence_at = time.monotonic()
                await emit_assistant_state(state="speaking", question=question, **who)
            spoken.append(sentence)
            await publish_bot_command(group_id, {"type": "say", "ask_id": ask_id, "text": sentence})
        await publish_bot_command(group_id, {"type": "say_end", "ask_id": ask_id})

        answer = " ".join(spoken).strip()
        if not answer:
            raise RuntimeError("empty answer")
        logger.info(
            "Ask Paw: answered %s in group %s (gather %.0fms, first sentence %.0fms, total %.0fms)",
            ask_id,
            group_id,
            (gathered - started) * 1000,
            ((first_sentence_at or gathered) - started) * 1000,
            (time.monotonic() - started) * 1000,
        )

        try:
            from pocketpaw_ee.cloud.livekit.service import post_call_bot_message

            await post_call_bot_message(
                group_id,
                f"**Q ({asked_by_name}):** {question}\n**Paw:** {answer}",
                "Paw",
            )
        except Exception:
            logger.warning("Ask Paw: could not post Q&A to group %s", group_id, exc_info=True)

        await emit_assistant_state(state="idle", question=question, answer=answer, **who)
        return answer
    except asyncio.CancelledError:
        logger.info("Ask Paw: %s in group %s was stopped", ask_id, group_id)
        raise
    except _AskStopped:
        logger.info("Ask Paw: %s in group %s was stopped or timed out", ask_id, group_id)
        await emit_assistant_state(state="idle", question=question, **who)
        return None
    except Exception:
        logger.warning("Ask Paw: answering %s in group %s failed", ask_id, group_id, exc_info=True)
        if not spoken:
            try:
                await publish_bot_command(
                    group_id, {"type": "say", "ask_id": ask_id, "text": _APOLOGY}
                )
            except Exception:
                logger.debug("Ask Paw: apology publish failed", exc_info=True)
        await emit_assistant_state(
            state="error",
            question=question,
            error="Paw couldn't answer that right now.",
            **who,
        )
        await emit_assistant_state(state="idle", question=question, **who)
        return None
    finally:
        try:
            await release_ask_lock(group_id, ask_id)
        except Exception:
            logger.warning("Ask Paw: could not release lock for group %s", group_id)


async def capture_spoken_question(group_id: str, ask_id: str, timeout: float = 4.0) -> str | None:
    """Wait for the bot to hand back the words spoken during a listen window."""
    key = REDIS_ASK_QUESTION_KEY.format(ask_id=ask_id)
    result = await _blocking_redis().blpop([key], timeout=timeout)
    if not result:
        logger.info("Ask Paw: no spoken question for %s in group %s", ask_id, group_id)
        return None
    return str(result[1])


async def answer_spoken_question(
    *,
    group_id: str,
    workspace_id: str,
    user_id: str,
    asked_by_name: str,
    ask_id: str,
) -> str | None:
    """Collect the question the bot heard, then answer it (or say it missed it)."""
    who = _who(group_id, ask_id, user_id, asked_by_name)
    try:
        question = (await capture_spoken_question(group_id, ask_id) or "").strip()
    except Exception:
        logger.warning("Ask Paw: reading spoken question %s failed", ask_id, exc_info=True)
        question = ""
    if not question:
        try:
            await publish_bot_command(
                group_id, {"type": "say", "ask_id": ask_id, "text": DIDNT_CATCH_THAT}
            )
            await emit_assistant_state(state="idle", **who)
        finally:
            await release_ask_lock(group_id, ask_id)
        return None
    return await answer_call_question(
        group_id=group_id,
        workspace_id=workspace_id,
        user_id=user_id,
        asked_by_name=asked_by_name,
        question=question[:500],
        ask_id=ask_id,
    )
