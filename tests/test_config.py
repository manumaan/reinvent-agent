from reinvent_agent import config


class StubCfn:
    def describe_stacks(self, StackName):  # noqa: N803
        if StackName == "ReinventAgentSearch":
            return {
                "Stacks": [{"Outputs": [{"OutputKey": "VectorBucketName", "OutputValue": "b"}]}]
            }
        raise RuntimeError("not deployed")


def test_stack_outputs_merges_deployed_stacks():
    assert config.stack_outputs("us-east-1", client=StubCfn()) == {"VectorBucketName": "b"}


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("REINVENT_VECTOR_BUCKET", "vb")
    monkeypatch.setenv("REINVENT_SESSIONS_TABLE", "tbl")
    config.settings.cache_clear()
    try:
        cfg = config.settings()
        assert (cfg.vector_bucket, cfg.sessions_table, cfg.region) == ("vb", "tbl", "us-east-1")
        assert cfg.model == "anthropic.claude-opus-5"
    finally:
        config.settings.cache_clear()
