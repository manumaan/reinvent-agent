"""Vector stores: Amazon S3 Vectors, and an in-memory store with the same filter
semantics for tests.

Filters use the S3 Vectors operator subset we need: ``$eq``, ``$ne``, ``$in``,
``$nin``, ``$gt``, ``$gte``, ``$lt``, ``$lte``, ``$and``, ``$or``. A bare value
means ``$eq``. On an array field, ``$eq``/``$in`` match if any element matches.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

PUT_BATCH = 500  # S3 Vectors PutVectors limit
MAX_TOP_K = 100


@dataclass
class Hit:
    key: str
    distance: float
    metadata: dict


class VectorStore(Protocol):
    def put(self, items: Sequence[tuple[str, list[float], dict]]) -> None: ...

    def query(self, vector: list[float], top_k: int, filter: dict | None = None) -> list[Hit]: ...


class S3VectorsStore:
    def __init__(self, bucket: str, index: str, region: str = "us-east-1", client=None):
        if client is None:
            import boto3

            client = boto3.client("s3vectors", region_name=region)
        self.client, self.bucket, self.index = client, bucket, index

    def put(self, items: Sequence[tuple[str, list[float], dict]]) -> None:
        for i in range(0, len(items), PUT_BATCH):
            self.client.put_vectors(
                vectorBucketName=self.bucket,
                indexName=self.index,
                vectors=[
                    {"key": k, "data": {"float32": v}, "metadata": m}
                    for k, v, m in items[i : i + PUT_BATCH]
                ],
            )

    def query(self, vector: list[float], top_k: int, filter: dict | None = None) -> list[Hit]:
        kwargs = dict(
            vectorBucketName=self.bucket,
            indexName=self.index,
            queryVector={"float32": vector},
            topK=min(top_k, MAX_TOP_K),
            returnMetadata=True,
            returnDistance=True,
        )
        if filter:
            kwargs["filter"] = filter
        resp = self.client.query_vectors(**kwargs)
        return [
            Hit(v["key"], v.get("distance", 0.0), v.get("metadata", {})) for v in resp["vectors"]
        ]


def _match_value(actual, op: str, expected) -> bool:
    values = actual if isinstance(actual, list) else [actual]
    if op == "$eq":
        return expected in values
    if op == "$ne":
        return expected not in values
    if op == "$in":
        return any(v in expected for v in values)
    if op == "$nin":
        return not any(v in expected for v in values)
    if actual is None or isinstance(actual, list):
        return False
    return {
        "$gt": actual > expected,
        "$gte": actual >= expected,
        "$lt": actual < expected,
        "$lte": actual <= expected,
    }[op]


def matches(meta: dict, flt: dict | None) -> bool:
    if not flt:
        return True
    for key, cond in flt.items():
        if key == "$and":
            if not all(matches(meta, c) for c in cond):
                return False
        elif key == "$or":
            if not any(matches(meta, c) for c in cond):
                return False
        else:
            if key not in meta:
                return False
            ops = cond if isinstance(cond, dict) else {"$eq": cond}
            if not all(_match_value(meta[key], op, v) for op, v in ops.items()):
                return False
    return True


class InMemoryVectorStore:
    def __init__(self):
        self.items: dict[str, tuple[list[float], dict]] = {}

    def put(self, items: Sequence[tuple[str, list[float], dict]]) -> None:
        for k, v, m in items:
            self.items[k] = (v, m)

    def query(self, vector: list[float], top_k: int, filter: dict | None = None) -> list[Hit]:
        def cosine_distance(a, b):
            dot = sum(x * y for x, y in zip(a, b, strict=True))
            na = math.sqrt(sum(x * x for x in a)) or 1.0
            nb = math.sqrt(sum(y * y for y in b)) or 1.0
            return 1 - dot / (na * nb)

        hits = [
            Hit(k, cosine_distance(vector, v), m)
            for k, (v, m) in self.items.items()
            if matches(m, filter)
        ]
        return sorted(hits, key=lambda h: h.distance)[: min(top_k, MAX_TOP_K)]
