"""Claude endpoints, kept behind one small interface.

Every provider speaks the same Anthropic Messages API (including the SDK tool runner),
so the rest of the code only needs ``make_client`` and a model ID. Adding or switching
endpoints happens here and in ``config`` -- nothing else changes.

Switching (e.g. once Bedrock grants Claude access to the account):

    reinvent-agent check-models --provider bedrock   # probe without switching
    reinvent-agent config set-provider bedrock       # persist the choice
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    name: str
    label: str
    model_prefix: str  # Bedrock IDs carry `anthropic.`; the Claude API uses bare IDs
    default_model: str  # bare ID
    # Probed by `check-models`, most preferred first (bare IDs). Opus 5 / Sonnet 5 are
    # not used (2026-09-27).
    candidates: tuple[str, ...] = ("claude-opus-4-8", "claude-opus-4-7", "claude-haiku-4-5")

    def model_id(self, model: str) -> str:
        """Full ID for this endpoint; accepts bare or already-prefixed IDs."""
        bare = model.removeprefix("anthropic.")
        return self.model_prefix + bare


PROVIDERS: dict[str, Provider] = {
    "bedrock": Provider("bedrock", "Claude on Amazon Bedrock", "anthropic.", "claude-opus-4-8"),
    "anthropic": Provider("anthropic", "Claude API (platform.claude.com)", "", "claude-opus-4-8"),
}


def get_provider(name: str) -> Provider:
    try:
        return PROVIDERS[name]
    except KeyError:
        raise ValueError(f"LLM provider must be one of {sorted(PROVIDERS)}, got {name!r}") from None


def make_client(region: str, provider: str = "bedrock", key_secret_id: str | None = None):
    """Claude via Amazon Bedrock (AWS credentials) or the Claude API (API key from
    ANTHROPIC_API_KEY or the AnthropicApiKey secret)."""
    get_provider(provider)
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
