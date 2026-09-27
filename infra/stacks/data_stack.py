"""Stateful resources shared by ingest, the agent tools and the reservation run."""

from aws_cdk import CfnOutput, RemovalPolicy, SecretValue, Stack
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as sm
from constructs import Construct

# Mirrors reinvent_agent.config.API_KEY_PLACEHOLDER.
API_KEY_PLACEHOLDER = "UNSET"


class DataStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Raw ListSessions pages, one prefix per event and ingest run.
        self.catalog_bucket = s3.Bucket(
            self,
            "CatalogBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Normalized sessions for exact lookups and the optimizer.
        self.sessions_table = ddb.TableV2(
            self,
            "SessionsTable",
            partition_key=ddb.Attribute(name="eventId", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="sessionId", type=ddb.AttributeType.STRING),
            billing=ddb.Billing.on_demand(),
            point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            global_secondary_indexes=[
                ddb.GlobalSecondaryIndexPropsV2(
                    index_name="byCode",
                    partition_key=ddb.Attribute(name="code", type=ddb.AttributeType.STRING),
                ),
                ddb.GlobalSecondaryIndexPropsV2(
                    index_name="byDayVenue",
                    partition_key=ddb.Attribute(name="eventDay", type=ddb.AttributeType.STRING),
                    sort_key=ddb.Attribute(name="venueStart", type=ddb.AttributeType.STRING),
                ),
            ],
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Preferences and approved plans (plan_version) per user.
        self.plans_table = ddb.TableV2(
            self,
            "PlansTable",
            partition_key=ddb.Attribute(name="userId", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="sk", type=ddb.AttributeType.STRING),
            billing=ddb.Billing.on_demand(),
            point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Builder ID tokens pushed by `reinvent-agent auth push-secret`. The cloud side
        # writes rotated refresh tokens back here, so readers also need PutSecretValue.
        self.token_secret = sm.Secret(
            self,
            "BuilderIdTokens",
            description="AWS Events API Builder ID tokens (access + rotating refresh token)",
            # Empty until a user pushes tokens; nothing sensitive in the initial value.
            secret_string_value=SecretValue.unsafe_plain_text("{}"),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Claude API key (platform.claude.com), used instead of Bedrock for Claude calls
        # when set. Created with a placeholder; set the real value out of band with
        # `reinvent-agent config set-api-key` so it never appears in code or templates.
        self.anthropic_api_key = sm.Secret(
            self,
            "AnthropicApiKey",
            description="Claude API key for reinvent-agent (`reinvent-agent config set-api-key`)",
            secret_string_value=SecretValue.unsafe_plain_text(API_KEY_PLACEHOLDER),
            removal_policy=RemovalPolicy.RETAIN,
        )

        CfnOutput(self, "CatalogBucketName", value=self.catalog_bucket.bucket_name)
        CfnOutput(self, "SessionsTableName", value=self.sessions_table.table_name)
        CfnOutput(self, "PlansTableName", value=self.plans_table.table_name)
        CfnOutput(self, "TokenSecretArn", value=self.token_secret.secret_arn)
        CfnOutput(self, "AnthropicApiKeySecretArn", value=self.anthropic_api_key.secret_arn)
