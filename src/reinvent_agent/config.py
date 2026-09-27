"""Runtime settings.

Explicit environment variables win. Anything unset is looked up once from the
deployed CloudFormation stacks' outputs (``ReinventAgentData``,
``ReinventAgentSearch``), so after `cdk deploy` no manual configuration is needed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

REGION = "us-east-1"
STACKS = ("ReinventAgentData", "ReinventAgentSearch")
# Claude Opus 5 / Sonnet 5 are not used (2026-09-27). Bedrock IDs carry an
# `anthropic.` prefix; the Claude API (Anthropic API key) uses bare IDs.
API_KEY_PLACEHOLDER = "UNSET"  # initial value of the AnthropicApiKey secret
DEFAULT_MODELS = {"bedrock": "anthropic.claude-opus-4-8", "anthropic": "claude-opus-4-8"}


@dataclass(frozen=True)
class Settings:
    region: str
    event_id: str
    vector_bucket: str | None
    vector_index: str
    sessions_table: str | None
    token_secret_arn: str | None
    anthropic_key_secret_arn: str | None
    # "bedrock" (Claude on Amazon Bedrock, AWS credentials) or "anthropic" (Claude API,
    # ANTHROPIC_API_KEY). Embeddings always use Bedrock Titan.
    llm_provider: str
    model: str


def stack_outputs(region: str, client=None) -> dict[str, str]:
    try:
        if client is None:
            import boto3

            client = boto3.client("cloudformation", region_name=region)
        out: dict[str, str] = {}
        for name in STACKS:
            try:
                stack = client.describe_stacks(StackName=name)["Stacks"][0]
            except Exception:  # stack not deployed yet
                continue
            out.update({o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])})
        return out
    except Exception:  # no credentials, no network: fall back to env/defaults
        return {}


def anthropic_api_key(secret_id: str | None, region: str = REGION, client=None) -> str | None:
    """ANTHROPIC_API_KEY from the environment, else the AnthropicApiKey secret.

    Returns None when neither is set (secret still holds the placeholder). The key is
    never stored on Settings, so it cannot leak through a printed config.
    """
    if key := os.environ.get("ANTHROPIC_API_KEY"):
        return key
    if not secret_id:
        return None
    value = _secret_value(secret_id, region, client)
    return None if value in (None, "", API_KEY_PLACEHOLDER) else value


@lru_cache(maxsize=4)
def _secret_value(secret_id: str, region: str, client=None) -> str | None:
    try:
        if client is None:
            import boto3

            client = boto3.client("secretsmanager", region_name=region)
        return client.get_secret_value(SecretId=secret_id)["SecretString"].strip()
    except Exception:  # no access or not deployed: behave as unset
        return None


def clear_caches() -> None:
    _secret_value.cache_clear()
    settings.cache_clear()


@lru_cache(maxsize=1)
def settings() -> Settings:
    env = os.environ.get
    region = env("REINVENT_REGION", REGION)
    explicit = (
        "REINVENT_VECTOR_BUCKET",
        "REINVENT_SESSIONS_TABLE",
        "REINVENT_ANTHROPIC_KEY_SECRET_ID",
    )
    outputs = {} if all(env(k) for k in explicit) else stack_outputs(region)
    key_secret = env("REINVENT_ANTHROPIC_KEY_SECRET_ID") or outputs.get("AnthropicApiKeySecretArn")
    provider = env("REINVENT_LLM_PROVIDER") or (
        "anthropic" if anthropic_api_key(key_secret, region) else "bedrock"
    )
    if provider not in DEFAULT_MODELS:
        raise ValueError(f"REINVENT_LLM_PROVIDER must be one of {sorted(DEFAULT_MODELS)}")
    return Settings(
        region=region,
        event_id=env("REINVENT_EVENT_ID", "reinvent2026"),
        vector_bucket=env("REINVENT_VECTOR_BUCKET") or outputs.get("VectorBucketName"),
        vector_index=env("REINVENT_VECTOR_INDEX") or outputs.get("VectorIndexName", "sessions"),
        sessions_table=env("REINVENT_SESSIONS_TABLE") or outputs.get("SessionsTableName"),
        token_secret_arn=env("REINVENT_TOKEN_SECRET_ID") or outputs.get("TokenSecretArn"),
        anthropic_key_secret_arn=key_secret,
        llm_provider=provider,
        model=env("REINVENT_MODEL", DEFAULT_MODELS[provider]),
    )
