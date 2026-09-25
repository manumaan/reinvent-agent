#!/usr/bin/env python3
import os

import aws_cdk as cdk
from stacks.data_stack import DataStack
from stacks.search_stack import SearchStack

app = cdk.App()
env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region="us-east-1",  # all resources live in us-east-1 (approved 2026-09-25)
)
DataStack(app, "ReinventAgentData", env=env)
SearchStack(app, "ReinventAgentSearch", env=env)
cdk.Tags.of(app).add("project", "reinvent-agent")
app.synth()
