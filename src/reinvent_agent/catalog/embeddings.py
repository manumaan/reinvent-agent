"""Text embeddings. Bedrock Titan v2 in production, a hashing embedder for tests."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

TITAN_V2 = "amazon.titan-embed-text-v2:0"
DIMENSION = 1024
_TITAN_MAX_CHARS = 40_000  # well under Titan v2's 8k-token input limit


class Embedder(Protocol):
    dimension: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class BedrockTitanEmbedder:
    def __init__(self, region: str = "us-east-1", client=None, workers: int = 8):
        if client is None:
            import boto3
            from botocore.config import Config

            # Indexing fires ~1,600 calls; ride out on-demand throttling instead of failing.
            retry = Config(retries={"mode": "adaptive", "max_attempts": 10})
            client = boto3.client("bedrock-runtime", region_name=region, config=retry)
        self.client = client
        self.dimension = DIMENSION
        self.workers = workers

    def _one(self, text: str) -> list[float]:
        body = {"inputText": text[:_TITAN_MAX_CHARS], "dimensions": DIMENSION, "normalize": True}
        resp = self.client.invoke_model(modelId=TITAN_V2, body=json.dumps(body))
        return json.loads(resp["body"].read())["embedding"]

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        with ThreadPoolExecutor(self.workers) as pool:
            return list(pool.map(self._one, texts))


class HashingEmbedder:
    """Deterministic bag-of-words hashing. No semantics beyond shared words, but
    lets tests and offline runs exercise the whole pipeline."""

    def __init__(self, dimension: int = 256):
        self.dimension = dimension

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * self.dimension
            for word in re.findall(r"[a-z0-9]+", text.lower()):
                h = int(hashlib.md5(word.encode()).hexdigest(), 16)
                vec[h % self.dimension] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out
