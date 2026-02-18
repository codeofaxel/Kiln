"""OpenRouter-specific integration for Kiln's agent loop.

Thin wrapper around :mod:`kiln.agent_loop` that pre-configures API
settings for `OpenRouter <https://openrouter.ai>`_ and provides a
curated catalog of models with their tool-calling capabilities.

Quick start::

    from kiln.openrouter import run_openrouter

    result = run_openrouter(
        "What is the printer status?",
        api_key="sk-or-...",
    )
    print(result.response)

The module also includes a simple REPL for interactive testing::

    KILN_OPENROUTER_KEY=sk-or-... python -m kiln.openrouter

Environment variables
---------------------
``KILN_OPENROUTER_KEY``
    API key for OpenRouter.  Required for :func:`run_openrouter`
    and :func:`agent_cli` unless passed explicitly.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from kiln.agent_loop import AgentConfig, AgentResult, run_agent_loop

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# ---------------------------------------------------------------------------
# Model catalog -- curated list with tool-calling tier and context window
# ---------------------------------------------------------------------------

KNOWN_MODELS: dict[str, dict[str, Any]] = {
    # ---- Tier: full (strong tool-calling) ----
    "anthropic/claude-sonnet-4": {"tier": "full", "context": 200_000},
    "anthropic/claude-opus-4": {"tier": "full", "context": 200_000},
    "openai/gpt-4o": {"tier": "full", "context": 128_000},
    "openai/gpt-4-turbo": {"tier": "full", "context": 128_000},
    "google/gemini-pro-1.5": {"tier": "full", "context": 1_000_000},
    "google/gemini-2.0-flash": {"tier": "full", "context": 1_000_000},
    # ---- Tier: standard (decent tool-calling) ----
    "openai/gpt-4o-mini": {"tier": "standard", "context": 128_000},
    "google/gemini-flash-1.5": {"tier": "standard", "context": 1_000_000},
    "cohere/command-r-plus": {"tier": "standard", "context": 128_000},
    "mistralai/mistral-large": {"tier": "standard", "context": 128_000},
    # ---- Tier: essential (basic tool-calling) ----
    "meta-llama/llama-3.1-70b-instruct": {"tier": "essential", "context": 128_000},
    "meta-llama/llama-3.1-8b-instruct": {"tier": "essential", "context": 128_000},
    "mistralai/mistral-7b-instruct": {"tier": "essential", "context": 32_000},
    "microsoft/phi-3-medium-128k-instruct": {"tier": "essential", "context": 128_000},
    "qwen/qwen-2-72b-instruct": {"tier": "essential", "context": 128_000},
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_supported_models() -> dict[str, dict[str, Any]]:
    """Return the known model catalog with tier and context info.

    Returns:
        A dict mapping model identifiers to their capability metadata.
        Each entry contains ``"tier"`` (``"full"``, ``"standard"``, or
        ``"essential"``) and ``"context"`` (context window size in tokens).
    """
    return dict(KNOWN_MODELS)


def get_model_tier(model: str) -> str:
    """Look up the tool-calling tier for a model.

    If the model is not in the known catalog, defaults to ``"standard"``.

    Args:
        model: OpenRouter model identifier (e.g. ``"openai/gpt-4o"``).

    Returns:
        ``"full"``, ``"standard"``, or ``"essential"``.
    """
    info = KNOWN_MODELS.get(model)
    if info:
        return info["tier"]
    # Unknown model -- assume standard capability
    logger.debug("Model %s not in known catalog, defaulting to 'standard' tier", model)
    return "standard"


def create_openrouter_config(
    api_key: str,
    model: str = "openai/gpt-4o",
    *,
    tool_tier: str | None = None,
    max_turns: int = 20,
    temperature: float = 0.1,
    system_prompt: str | None = None,
    timeout: int = 120,
) -> AgentConfig:
    """Create an :class:`AgentConfig` pre-configured for OpenRouter.

    If ``tool_tier`` is ``None``, it is auto-detected from the model's
    entry in :data:`KNOWN_MODELS`.  Unknown models default to
    ``"standard"``.

    Args:
        api_key: OpenRouter API key.
        model: Model identifier from OpenRouter's catalog.
        tool_tier: Override the tool tier instead of auto-detecting.
        max_turns: Maximum tool-call round-trips.
        temperature: Sampling temperature.
        system_prompt: Custom system prompt (or ``None`` for default).
        timeout: Seconds per API call.

    Returns:
        A fully configured :class:`AgentConfig`.
    """
    resolved_tier = tool_tier if tool_tier is not None else get_model_tier(model)

    return AgentConfig(
        api_key=api_key,
        base_url=OPENROUTER_BASE_URL,
        model=model,
        tool_tier=resolved_tier,
        max_turns=max_turns,
        temperature=temperature,
        system_prompt=system_prompt,
        timeout=timeout,
    )


def run_openrouter(
    prompt: str,
    api_key: str,
    model: str = "openai/gpt-4o",
    **kwargs: Any,
) -> AgentResult:
    """Convenience function: create config and run agent loop in one call.

    All extra keyword arguments are forwarded to
    :func:`create_openrouter_config`.

    Args:
        prompt: The user message.
        api_key: OpenRouter API key.
        model: Model identifier.
        **kwargs: Forwarded to :func:`create_openrouter_config`.

    Returns:
        The :class:`AgentResult` from the agent loop.
    """
    config = create_openrouter_config(api_key=api_key, model=model, **kwargs)
    return run_agent_loop(prompt, config)


# ---------------------------------------------------------------------------
# Interactive REPL for testing
# ---------------------------------------------------------------------------


def agent_cli() -> None:
    """Simple REPL for testing the agent loop from the terminal.

    Reads the API key from ``KILN_OPENROUTER_KEY`` environment variable.
    Optionally accepts a model name as the first CLI argument::

        KILN_OPENROUTER_KEY=sk-or-... python -m kiln.openrouter gpt-4o-mini

    Type ``quit`` or ``exit`` to leave, ``model`` to show current model,
    or ``models`` to list the known catalog.
    """
    api_key = os.environ.get("KILN_OPENROUTER_KEY", "")
    if not api_key:
        print("Error: KILN_OPENROUTER_KEY environment variable is not set.")
        print("Set it to your OpenRouter API key to use the agent CLI.")
        sys.exit(1)

    # Optional model argument
    model = "openai/gpt-4o"
    args = sys.argv[1:]
    if args:
        candidate = args[0]
        # Accept bare names like "gpt-4o" or full paths like "openai/gpt-4o"
        if "/" not in candidate:
            # Try to match against known models
            matches = [k for k in KNOWN_MODELS if k.endswith(f"/{candidate}")]
            if matches:
                model = matches[0]
            else:
                model = candidate  # pass through as-is
        else:
            model = candidate

    tier = get_model_tier(model)
    config = create_openrouter_config(api_key=api_key, model=model)

    print(f"Kiln Agent CLI -- model: {model} (tier: {tier})")
    print("Type 'quit' to exit, 'models' to list known models.\n")

    conversation = None  # type: list | None

    while True:
        try:
            prompt = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        if not prompt:
            continue
        if prompt.lower() in ("quit", "exit", "q"):
            print("Goodbye.")
            break
        if prompt.lower() == "models":
            print("\nKnown models:")
            for name, info in sorted(KNOWN_MODELS.items()):
                marker = "*" if name == model else " "
                print(f"  {marker} {name}  (tier: {info['tier']}, ctx: {info['context']:,})")
            print()
            continue
        if prompt.lower() == "model":
            print(f"Current model: {model} (tier: {tier})")
            continue
        if prompt.lower() == "reset":
            conversation = None
            print("Conversation reset.\n")
            continue

        try:
            result = run_agent_loop(
                prompt,
                config,
                conversation=conversation,
            )
            print(f"\nassistant> {result.response}")
            print(
                f"  [{result.turns} turn(s), "
                f"{result.tool_calls_made} tool call(s)"
                f"{f', {result.total_tokens} tokens' if result.total_tokens else ''}]\n"
            )
            # Continue the conversation
            conversation = result.messages
        except Exception as exc:
            print(f"\nError: {exc}\n")


# ---------------------------------------------------------------------------
# Allow running as a module: python -m kiln.openrouter
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    agent_cli()
