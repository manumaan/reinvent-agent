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


def test_secrets(template):
    template.resource_count_is("AWS::SecretsManager::Secret", 2)
    # The API key secret is created with a placeholder, never a real key.
    template.has_resource_properties(
        "AWS::SecretsManager::Secret", {"SecretString": "UNSET", "Description": Match.any_value()}
    )


def test_placeholder_matches_application():
    from stacks.data_stack import API_KEY_PLACEHOLDER

    from reinvent_agent.config import API_KEY_PLACEHOLDER as APP_PLACEHOLDER

    assert API_KEY_PLACEHOLDER == APP_PLACEHOLDER


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


def test_reservation_stack_schedules_lambda_and_topic():
    from stacks.reservation_stack import ReservationStack

    from reinvent_agent.reservations import SCHEDULE

    app = App(context={"aws:cdk:bundling-stacks": []})  # don't build the Lambda package
    data = DataStack(app, "D")
    t = Template.from_stack(ReservationStack(app, "R", data=data))
    t.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "reinvent_agent.lambda_handler.handler",
            "Timeout": 900,
            # New accounts can't reserve concurrency (10 total, all unreserved).
            "ReservedConcurrentExecutions": Match.absent(),
        },
    )
    t.resource_count_is("AWS::SNS::Topic", 1)
    t.resource_count_is("AWS::Scheduler::Schedule", len(SCHEDULE))
    t.has_resource_properties(
        "AWS::Scheduler::Schedule",
        {
            "ScheduleExpression": "rate(2 minutes)",
            "StartDate": "2026-10-08T07:00:00Z",  # midnight PDT
            "EndDate": "2026-10-09T07:00:00Z",
            "ScheduleExpressionTimezone": "America/Los_Angeles",
            "FlexibleTimeWindow": {"Mode": "OFF"},
            "Target": Match.object_like({"Input": '{"action": "poll", "label": "API opening"}'}),
        },
    )
