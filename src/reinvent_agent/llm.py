"""Claude endpoints, kept behind one small interface.

Every provider speaks the same Anthropic Messages API (including the SDK tool runner),
so the rest of the code only needs ``make_client`` and a model ID. Adding or switching
endpoints happens here and in ``config`` -- nothing else changes.

OpenRouter is the default: the normal Anthropic SDK pointed at its Anthropic-compatible
address. Switching:

    reinvent-agent check-models --provider bedrock   # probe without switching
    reinvent-agent config set-provider bedrock       # persist the choice
"""

from __future__ import annotations

import re
from dataclasses import dataclass

OPENROUTER_BASE_URL = "https://openrouter.ai/api"


@dataclass(frozen=True)
class Provider:
    name: str
    label: str
    # Bedrock IDs carry `anthropic.`, OpenRouter's `anthropic/`; the Claude API uses bare IDs
    model_prefix: str
    default_model: str  # bare ID
    # Probed by `check-models`, most preferred first (bare IDs). Opus 5 / Sonnet 5 are
    # not used (2026-09-27).
    candidates: tuple[str, ...] = ("claude-opus-4-8", "claude-opus-4-7", "claude-haiku-4-5")
    dotted_version: bool = False  # OpenRouter writes claude-opus-4-8 as claude-opus-4.8

    def model_id(self, model: str) -> str:
        """Full ID for this endpoint; accepts bare or already-prefixed IDs."""
        bare = model.removeprefix("anthropic.").removeprefix("anthropic/")
        if self.dotted_version:
            bare = re.sub(r"-(\d+)-(\d+)$", r"-\1.\2", bare)
        return self.model_prefix + bare


PROVIDERS: dict[str, Provider] = {
    "openrouter": Provider(
        "openrouter", "Claude via OpenRouter", "anthropic/", "claude-opus-4-8", dotted_version=True
    ),
    "bedrock": Provider("bedrock", "Claude on Amazon Bedrock", "anthropic.", "claude-opus-4-8"),
    "anthropic": Provider("anthropic", "Claude API (platform.claude.com)", "", "claude-opus-4-8"),
}


def get_provider(name: str) -> Provider:
    try:
        return PROVIDERS[name]
    except KeyError:
        raise ValueError(f"LLM provider must be one of {sorted(PROVIDERS)}, got {name!r}") from None


def make_client(region: str, provider: str = "bedrock", key_secret_id: str | None = None):
    """Claude via OpenRouter (key from OPENROUTER_API_KEY or its secret), Amazon Bedrock
    (AWS credentials) or the Claude API (ANTHROPIC_API_KEY or the AnthropicApiKey secret)."""
    get_provider(provider)
    if provider == "openrouter":
        from anthropic import Anthropic

        from reinvent_agent.config import openrouter_api_key

        key = openrouter_api_key(region)
        if not key:
            raise RuntimeError(
                "No OpenRouter key: set OPENROUTER_API_KEY, or check you can read the secret "
                "named by REINVENT_OPENROUTER_SECRET_ID."
            )
        return Anthropic(api_key=key, base_url=OPENROUTER_BASE_URL)
    if provider == "anthropic":
        from anthropic import Anthropic

        from reinvent_agent.config import anthropic_api_key

        key = anthropic_api_key(key_secret_id, region)
        if not key:
            raise RuntimeError(
                "No Claude API key: set ANTHROPIC_API_KEY or run "
                "`reinvent-agent config set-api-key`."
            )
        return Anthropic(api_key=key)
    from anthropic import AnthropicBedrockMantle

    return AnthropicBedrockMantle(aws_region=region)
