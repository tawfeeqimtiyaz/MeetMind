"""Chat model providers, tried in order: NVIDIA -> Groq -> local Ollama.

LangChain gives every provider the same interface (`.invoke(messages)`,
`.with_structured_output(Schema)`), so swapping models is configuration, not code.

We run the fallback loop ourselves (instead of `Runnable.with_fallbacks`) so we can
record *which* model produced each result and skip models whose context window is too small.
"""
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from langchain_core.language_models import BaseChatModel

from ..config import Settings

log = logging.getLogger(__name__)

T = TypeVar("T")

CHARS_PER_TOKEN = 3.5  # rough estimate, good enough to decide whether input fits


class AIUnavailableError(RuntimeError):
    """No provider configured, or every provider failed. The message is safe to show users."""


@dataclass(frozen=True)
class Provider:
    name: str                          # stored as Meeting.ai_model, e.g. "groq:openai/gpt-oss-120b"
    build: Callable[[], BaseChatModel]
    max_input_tokens: int
    structured_method: str | None = None  # how this provider is asked for JSON output

    def fits(self, input_chars: int) -> bool:
        return input_chars / CHARS_PER_TOKEN <= self.max_input_tokens


# ---------- provider factories (imports are lazy so unused SDKs cost nothing) ----------

def _nvidia(settings: Settings, model: str) -> Provider:
    def build():
        from langchain_nvidia_ai_endpoints import ChatNVIDIA

        return ChatNVIDIA(
            model=model, api_key=settings.nvidia_api_key, base_url=settings.nvidia_base_url,
            temperature=0.2, max_tokens=8192,
        )

    return Provider(f"nvidia:{model}", build, max_input_tokens=100_000)


def _groq(settings: Settings, model: str, reasoning_effort: str) -> Provider:
    def build():
        from langchain_groq import ChatGroq

        return ChatGroq(
            model=model, api_key=settings.groq_api_key, temperature=0.2,
            reasoning_effort=reasoning_effort, max_tokens=8192,
            timeout=settings.ai_timeout_seconds, max_retries=2,
        )

    return Provider(f"groq:{model}", build, max_input_tokens=100_000, structured_method="json_schema")


def _ollama(settings: Settings) -> Provider:
    def build():
        from langchain_ollama import ChatOllama

        return ChatOllama(
            model=settings.ollama_model, base_url=settings.ollama_base_url,
            temperature=0, num_ctx=settings.ollama_num_ctx, reasoning=False, keep_alive="10m",
            # CPU inference is slow: allow much longer than the cloud providers
            client_kwargs={"timeout": settings.ai_timeout_seconds * 5},
        )

    # keep ~4k tokens of the context window free for the answer
    return Provider(
        f"ollama:{settings.ollama_model}", build,
        max_input_tokens=max(settings.ollama_num_ctx - 4096, 1024), structured_method="json_schema",
    )


def analysis_providers(settings: Settings) -> list[Provider]:
    providers = []
    if settings.nvidia_api_key:
        providers.append(_nvidia(settings, settings.nvidia_analysis_model))
    if settings.groq_api_key:
        providers.append(_groq(settings, settings.groq_analysis_model, "medium"))
    if settings.ollama_model:
        providers.append(_ollama(settings))
    return providers


def chat_providers(settings: Settings) -> list[Provider]:
    providers = []
    if settings.nvidia_api_key:
        providers.append(_nvidia(settings, settings.nvidia_chat_model))
    if settings.groq_api_key:
        providers.append(_groq(settings, settings.groq_chat_model, "low"))
    if settings.ollama_model:
        providers.append(_ollama(settings))
    return providers


# ---------- fallback loop ----------

def run_with_fallback(
    providers: list[Provider], call: Callable[[Provider], T], *, purpose: str, input_chars: int,
) -> tuple[T, str]:
    """Try each provider in order; return (result, provider name) from the first that succeeds."""
    if not providers:
        raise AIUnavailableError(
            "No AI provider is configured. Add NVIDIA_API_KEY or GROQ_API_KEY (or OLLAMA_MODEL) to .env."
        )

    failures = []
    for p in providers:
        if not p.fits(input_chars):
            log.info("%s: skipping %s, input (~%d tokens) exceeds its context window",
                     purpose, p.name, input_chars / CHARS_PER_TOKEN)
            failures.append(f"{p.name}: input too long")
            continue
        started = time.monotonic()
        try:
            result = call(p)
        except Exception as exc:  # noqa: BLE001 - any provider error means "try the next one"
            log.warning("%s: %s failed after %.1fs: %s: %s",
                        purpose, p.name, time.monotonic() - started, type(exc).__name__, exc)
            failures.append(f"{p.name}: {type(exc).__name__}")
            continue
        log.info("%s: answered by %s in %.1fs", purpose, p.name, time.monotonic() - started)
        return result, p.name

    log.error("%s: every provider failed: %s", purpose, "; ".join(failures))
    raise AIUnavailableError("The AI service is unavailable right now. Please try again later.")


def message_text(message) -> str:
    """Plain text of a chat model reply (content can be a string or a list of parts)."""
    text = getattr(message, "text", None)  # a property in langchain-core 1.x
    if isinstance(text, str):
        return str(text).strip()
    content = message.content
    if isinstance(content, list):
        content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return str(content).strip()
