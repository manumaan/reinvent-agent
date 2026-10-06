"""The unattended reservation run: Lambda + EventBridge Scheduler jobs + SNS.

Jobs come from ``reinvent_agent.reservations.SCHEDULE``: a sign-in reminder on Oct 7
and a poll every 2 minutes through Oct 8 (PDT), the day the Events API opens for
reservations (time unannounced). Seats open in the re:Invent portal on Oct 6, by hand.
The Lambda package is built locally with uv (no Docker): our package plus httpx,
pydantic and tzdata as manylinux wheels; boto3 comes with the Lambda runtime.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC
from pathlib import Path

import jsii
from aws_cdk import BundlingOptions, CfnOutput, DockerImage, Duration, ILocalBundling, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_sns as sns
from constructs import Construct

from reinvent_agent.reservations import SCHEDULE

REPO = Path(__file__).resolve().parents[2]
LAMBDA_DEPS = ["httpx", "pydantic", "tzdata"]


@jsii.implements(ILocalBundling)
class UvBundling:
    def try_bundle(self, output_dir: str, *args, **kwargs) -> bool:
        uv = shutil.which("uv")
        if not uv:
            return False  # fall back to the Docker command
        cfile = Path(output_dir).parent / "reinvent-agent-constraints.txt"
        subprocess.run(
            [
                uv, "export", "--no-dev", "--no-hashes", "--no-emit-project", "--frozen",
                "--quiet", "--output-file", str(cfile),
            ],
            cwd=REPO,
            check=True,
        )  # fmt: skip
        result = subprocess.run(
            [
                uv, "pip", "install", "--quiet", "--target", output_dir,
                "--python-platform", "x86_64-manylinux2014", "--python-version", "3.12",
                "--only-binary", ":all:", "-c", str(cfile), *LAMBDA_DEPS,
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
        )  # fmt: skip
        if result.returncode:
            raise RuntimeError(f"uv pip install failed: {result.stderr[-2000:]}")
        shutil.copytree(
            REPO / "src" / "reinvent_agent",
            Path(output_dir) / "reinvent_agent",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            dirs_exist_ok=True,
        )
        return True


class ReservationStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, data, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.topic = sns.Topic(
            self, "ReservationNotices", display_name="re:Invent reservations", enforce_ssl=True
        )

        self.function = lambda_.Function(
            self,
            "ReservationRun",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            handler="reinvent_agent.lambda_handler.handler",
            code=lambda_.Code.from_asset(
                str(REPO / "src"),
                bundling=BundlingOptions(
                    image=DockerImage.from_registry("public.ecr.aws/sam/build-python3.12"),
                    local=UvBundling(),
                    command=[
                        "bash",
                        "-c",
                        f"pip install -t /asset-output {' '.join(LAMBDA_DEPS)} && "
                        "cp -r /asset-input/reinvent_agent /asset-output/",
                    ],
                ),  # fmt: skip
            ),
            timeout=Duration.minutes(15),  # polls until release, then paces 30 sessions/min
            memory_size=512,
            # No reserved concurrency: new accounts have a 10-execution limit that must
            # stay unreserved. One run at a time is enforced by a DynamoDB lease instead.
            log_retention=logs.RetentionDays.ONE_MONTH,
            environment={
                "TOKEN_SECRET_ARN": data.token_secret.secret_arn,
                "PLANS_TABLE": data.plans_table.table_name,
                "TOPIC_ARN": self.topic.topic_arn,
                "EVENT_ID": "reinvent2026",
            },
        )
        # Reads the tokens and writes rotated refresh tokens back.
        data.token_secret.grant_read(self.function)
        data.token_secret.grant_write(self.function)
        data.plans_table.grant_read_write_data(self.function)
        self.topic.grant_publish(self.function)

        role = iam.Role(
            self, "SchedulerRole", assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com")
        )
        self.function.grant_invoke(role)
        utc = "%Y-%m-%dT%H:%M:%SZ"
        for job in SCHEDULE:
            if job.at:
                timing = {"schedule_expression": f"at({job.at:%Y-%m-%dT%H:%M:%S})"}
            else:
                timing = {
                    "schedule_expression": f"rate({job.every_minutes} minutes)",
                    "start_date": job.start.astimezone(UTC).strftime(utc),
                    "end_date": job.end.astimezone(UTC).strftime(utc),
                }
            scheduler.CfnSchedule(
                self,
                f"Schedule-{job.name}",
                name=f"reinvent-reservations-{job.name}",
                description=f"re:Invent reservations: {job.label} ({job.action})",
                schedule_expression_timezone="America/Los_Angeles",
                flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
                target=scheduler.CfnSchedule.TargetProperty(
                    arn=self.function.function_arn,
                    role_arn=role.role_arn,
                    input=json.dumps({"action": job.action, "label": job.label}),
                    retry_policy=scheduler.CfnSchedule.RetryPolicyProperty(
                        maximum_retry_attempts=0  # the poll itself retries every 2 minutes
                    ),
                ),
                **timing,
            )

        CfnOutput(self, "ReservationTopicArn", value=self.topic.topic_arn)
        CfnOutput(self, "ReservationFunctionName", value=self.function.function_name)
