#!/usr/bin/env python3
import os

import aws_cdk as cdk
from stacks.data_stack import DataStack

app = cdk.App()
env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)
DataStack(app, "ReinventAgentData", env=env)
cdk.Tags.of(app).add("project", "reinvent-agent")
app.synth()
