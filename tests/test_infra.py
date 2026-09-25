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
