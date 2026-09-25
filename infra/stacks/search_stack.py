"""S3 Vectors bucket + index for catalog search (design §4)."""

from aws_cdk import Aws, CfnOutput, Stack
from aws_cdk import aws_s3vectors as s3v
from constructs import Construct

# Mirrors reinvent_agent.catalog.documents / embeddings (kept literal so the infra
# app does not import the application package).
DIMENSION = 1024
NON_FILTERABLE_KEYS = ["title", "snippet", "room", "speakers"]
INDEX_NAME = "sessions"


class SearchStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        bucket = s3v.CfnVectorBucket(
            self, "CatalogVectors", vector_bucket_name=f"reinvent-agent-{Aws.ACCOUNT_ID}"
        )
        s3v.CfnIndex(
            self,
            "SessionsIndex",
            vector_bucket_arn=bucket.attr_vector_bucket_arn,  # implicit dependency
            index_name=INDEX_NAME,
            data_type="float32",
            dimension=DIMENSION,
            distance_metric="cosine",
            metadata_configuration=s3v.CfnIndex.MetadataConfigurationProperty(
                non_filterable_metadata_keys=NON_FILTERABLE_KEYS
            ),
        )

        CfnOutput(self, "VectorBucketName", value=bucket.vector_bucket_name)
        CfnOutput(self, "VectorIndexName", value=INDEX_NAME)
