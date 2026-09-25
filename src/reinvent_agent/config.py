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


@dataclass(frozen=True)
class Settings:
    region: str
    event_id: str
    vector_bucket: str | None
    vector_index: str
    sessions_table: str | None
    token_secret_arn: str | None
    # Claude on Amazon Bedrock (Anthropic SDK Mantle client): `anthropic.`-prefixed IDs.
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


@lru_cache(maxsize=1)
def settings() -> Settings:
    env = os.environ.get
    region = env("REINVENT_REGION", REGION)
    needs_lookup = not (env("REINVENT_VECTOR_BUCKET") and env("REINVENT_SESSIONS_TABLE"))
    outputs = stack_outputs(region) if needs_lookup else {}
    return Settings(
        region=region,
        event_id=env("REINVENT_EVENT_ID", "reinvent2026"),
        vector_bucket=env("REINVENT_VECTOR_BUCKET") or outputs.get("VectorBucketName"),
        vector_index=env("REINVENT_VECTOR_INDEX") or outputs.get("VectorIndexName", "sessions"),
        sessions_table=env("REINVENT_SESSIONS_TABLE") or outputs.get("SessionsTableName"),
        token_secret_arn=env("REINVENT_TOKEN_SECRET_ID") or outputs.get("TokenSecretArn"),
        model=env("REINVENT_MODEL", "anthropic.claude-opus-5"),
    )
