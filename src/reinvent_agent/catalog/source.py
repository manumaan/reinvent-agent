"""Where the dumped catalog lives: a local JSONL file and/or the catalog S3 bucket.

The catalog is registration-gated, so it never goes into git. The deployed
``CatalogBucket`` holds the shared copy at ``catalog/<event_id>/catalog.jsonl``;
``catalog index`` reads it from there when no local file is given.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from pathlib import Path

from reinvent_agent.events_api.models import Session


def local_path(event_id: str) -> Path:
    return Path("fixtures") / event_id / "catalog.jsonl"


def s3_key(event_id: str) -> str:
    return f"catalog/{event_id}/catalog.jsonl"


def s3_uri(bucket: str, event_id: str) -> str:
    return f"s3://{bucket}/{s3_key(event_id)}"


def _split_s3(uri: str) -> tuple[str, str]:
    bucket, _, key = uri.removeprefix("s3://").partition("/")
    if not bucket or not key:
        raise ValueError(f"expected s3://bucket/key, got {uri!r}")
    return bucket, key


def read_text(location: str | Path, s3=None) -> str:
    """Contents of a local path or an ``s3://bucket/key`` URI."""
    location = str(location)
    if not location.startswith("s3://"):
        return Path(location).read_text()
    if s3 is None:
        import boto3

        s3 = boto3.client("s3")
    bucket, key = _split_s3(location)
    return s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")


def load_sessions(location: str | Path, s3=None) -> list[Session]:
    return [
        Session.model_validate_json(x) for x in read_text(location, s3).splitlines() if x.strip()
    ]


def to_jsonl(raw_sessions: Iterable[dict]) -> str:
    """Normalize API items (validated, API field names, no nulls) to JSONL."""
    lines = []
    for raw in raw_sessions:
        s = Session.model_validate(raw)
        lines.append(json.dumps(s.model_dump(mode="json", by_alias=True, exclude_none=True)))
    return "".join(line + "\n" for line in lines)


def upload(body: str, bucket: str, event_id: str, s3=None) -> str:
    if s3 is None:
        import boto3

        s3 = boto3.client("s3")
    key = s3_key(event_id)
    s3.put_object(
        Bucket=bucket, Key=key, Body=body.encode("utf-8"), ContentType="application/x-ndjson"
    )
    return f"s3://{bucket}/{key}"


def download_catalog(
    client, event_id: str, progress: Callable[[int, int | None], None] | None = None
) -> tuple[list[dict], int | None]:
    """Walk ListSessions (needs a Builder ID sign-in). Returns (raw items, totalCount)."""
    items: list[dict] = []
    total = None
    for page in client.iter_session_pages(event_id, include_abstracts=True):
        total = page.get("totalCount", total)
        items.extend(page["items"])
        if progress:
            progress(len(items), total)
    return items, total


def resolve(file: str | Path | None, event_id: str, bucket: str | None) -> str:
    """Explicit file/URI wins; else the local default if present; else the bucket copy."""
    if file:
        return str(file)
    local = local_path(event_id)
    if local.exists() or not bucket:
        return str(local)
    return s3_uri(bucket, event_id)


# --- archive: every session ever seen, so withdrawn favorites keep a name -----------


def archive_path(event_id: str) -> Path:
    return Path("fixtures") / event_id / "catalog-archive.json"


def load_archive(event_id: str) -> dict[str, dict]:
    """``{sessionId: {"code", "title"}}`` for every session any download has contained."""
    try:
        return json.loads(archive_path(event_id).read_text())
    except (OSError, ValueError):
        return {}


def update_archive(event_id: str, sessions: Iterable[Session]) -> int:
    """Merge sessions into the archive (never removes). Returns how many were new."""
    archive = load_archive(event_id)
    before = len(archive)
    for s in sessions:
        archive[s.session_id] = {"code": s.code, "title": s.title}
    path = archive_path(event_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(archive, sort_keys=True))
    return len(archive) - before
