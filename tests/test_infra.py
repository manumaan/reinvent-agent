import sys
from pathlib import Path

import pytest

pytest.importorskip("aws_cdk")

from aws_cdk import App  # noqa: E402
from aws_cdk.assertions import Match, Template  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent.parent / "infra"))
from stacks.data_stack import DataStack  # noqa: E402


@pytest.fixture(scope="module")
def template() -> Template:
    return Template.from_stack(DataStack(App(), "Test"))


def test_bucket_is_private_and_encrypted(template):
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
            "BucketEncryption": Match.any_value(),
        },
    )


def test_tables(template):
    template.resource_count_is("AWS::DynamoDB::GlobalTable", 2)
    template.has_resource_properties(
        "AWS::DynamoDB::GlobalTable",
        {"GlobalSecondaryIndexes": Match.array_with([Match.object_like({"IndexName": "byCode"})])},
    )


def test_token_secret(template):
    template.resource_count_is("AWS::SecretsManager::Secret", 1)


def test_search_stack_vector_index():
    from stacks.search_stack import SearchStack

    t = Template.from_stack(SearchStack(App(), "TestSearch"))
    t.resource_count_is("AWS::S3Vectors::VectorBucket", 1)
    t.has_resource_properties(
        "AWS::S3Vectors::Index",
        {
            "Dimension": 1024,
            "DistanceMetric": "cosine",
            "DataType": "float32",
            "MetadataConfiguration": {
                "NonFilterableMetadataKeys": ["title", "snippet", "room", "speakers"]
            },
        },
    )


def test_index_config_matches_application():
    from stacks.search_stack import DIMENSION, NON_FILTERABLE_KEYS

    from reinvent_agent.catalog.documents import NON_FILTERABLE_KEYS as APP_KEYS
    from reinvent_agent.catalog.embeddings import DIMENSION as APP_DIM

    assert (DIMENSION, NON_FILTERABLE_KEYS) == (APP_DIM, APP_KEYS)
