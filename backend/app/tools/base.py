"""Resilient tool calls: timeout, one retry with backoff, then a structured error (never an exception)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime


class ToolError(Exception):
    """A tool failed in an expected way (bad input, service down, not configured)."""


class ToolInputError(ToolError):
    """The caller passed bad arguments; retrying the same call would not help."""


async def call_tool(
    deps, agent: str, name: str, fn: Callable[..., Awaitable[dict]], args: dict,
    timeout: float = 90.0, retries: int = 1, backoff: float = 1.5,
) -> dict:
    """Run ``fn(**args)`` and always return a dict: ``{"ok": True, ...result}`` or ``{"ok": False, "error": ...}``.

    Failures are written to the trace; the calling agent decides how to adapt.
    """
    last_error = ""
    for attempt in range(retries + 1):
        try:
            result = await asyncio.wait_for(fn(**args), timeout=timeout)
            await deps.log(agent, "tool_result", {"tool": name, "attempt": attempt + 1, "result": _short(result)})
            return {"ok": True, **result}
        except (ToolInputError, TypeError, ValueError, KeyError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            break  # bad input: retrying the same call is pointless
        except TimeoutError:
            last_error = f"timed out after {timeout:.0f}s"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < retries:
            await asyncio.sleep(backoff * (attempt + 1))
    error = {"agent": agent, "tool": name, "error": last_error, "ts": datetime.now(UTC).isoformat()}
    await deps.log(agent, "tool_error", error)
    return {"ok": False, "error": last_error}


def _short(result: dict, limit: int = 2000) -> dict:
    """Keep trace rows readable: truncate very long values."""
    text = str(result)
    return result if len(text) <= limit else {"truncated": text[:limit] + "..."}
