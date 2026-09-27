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
    monkeypatch.setenv("REINVENT_ANTHROPIC_KEY_SECRET_ID", "arn:key")
    monkeypatch.setattr(config, "_secret_value", lambda sid, region, client=None: "UNSET")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("REINVENT_LLM_PROVIDER", raising=False)
    config.settings.cache_clear()
    try:
        cfg = config.settings()
        assert (cfg.vector_bucket, cfg.sessions_table, cfg.region) == ("vb", "tbl", "us-east-1")
        assert (cfg.llm_provider, cfg.model) == ("bedrock", "anthropic.claude-opus-4-8")
    finally:
        config.settings.cache_clear()


def test_api_key_selects_claude_api(monkeypatch):
    monkeypatch.setenv("REINVENT_VECTOR_BUCKET", "vb")
    monkeypatch.setenv("REINVENT_SESSIONS_TABLE", "tbl")
    monkeypatch.setenv("REINVENT_ANTHROPIC_KEY_SECRET_ID", "arn:key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("REINVENT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("REINVENT_MODEL", raising=False)
    config.settings.cache_clear()
    try:
        cfg = config.settings()
        assert (cfg.llm_provider, cfg.model) == ("anthropic", "claude-opus-4-8")
        monkeypatch.setenv("REINVENT_LLM_PROVIDER", "bedrock")  # explicit choice wins
        config.settings.cache_clear()
        assert config.settings().llm_provider == "bedrock"
    finally:
        config.settings.cache_clear()


def test_make_client_per_provider(monkeypatch):
    from anthropic import Anthropic, AnthropicBedrockMantle

    from reinvent_agent.qa import make_client

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "y")
    assert isinstance(make_client("us-east-1", "anthropic"), Anthropic)
    assert isinstance(make_client("us-east-1", "bedrock"), AnthropicBedrockMantle)


class StubSecrets:
    def __init__(self, value):
        self.value, self.calls = value, 0

    def get_secret_value(self, SecretId):  # noqa: N803
        self.calls += 1
        return {"SecretString": self.value}


def test_api_key_from_secret(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config.clear_caches()
    stub = StubSecrets("sk-ant-from-secret\n")
    assert config.anthropic_api_key("arn:key", client=stub) == "sk-ant-from-secret"
    config.clear_caches()
    assert config.anthropic_api_key("arn:key", client=StubSecrets("UNSET")) is None
    assert config.anthropic_api_key(None) is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")  # env wins, no secret read
    other = StubSecrets("sk-ant-from-secret")
    assert config.anthropic_api_key("arn:other", client=other) == "sk-ant-env"
    assert other.calls == 0
    config.clear_caches()


def test_secret_key_selects_claude_api(monkeypatch):
    monkeypatch.setenv("REINVENT_VECTOR_BUCKET", "vb")
    monkeypatch.setenv("REINVENT_SESSIONS_TABLE", "tbl")
    monkeypatch.setenv("REINVENT_ANTHROPIC_KEY_SECRET_ID", "arn:key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("REINVENT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("REINVENT_MODEL", raising=False)
    monkeypatch.setattr(config, "_secret_value", lambda sid, region, client=None: "sk-ant-x")
    config.settings.cache_clear()
    try:
        cfg = config.settings()
        assert (cfg.llm_provider, cfg.model) == ("anthropic", "claude-opus-4-8")
        assert "sk-ant" not in repr(cfg)
    finally:
        config.settings.cache_clear()


def test_saved_provider_and_model(monkeypatch):
    monkeypatch.setenv("REINVENT_VECTOR_BUCKET", "vb")
    monkeypatch.setenv("REINVENT_SESSIONS_TABLE", "tbl")
    monkeypatch.setenv("REINVENT_ANTHROPIC_KEY_SECRET_ID", "arn:key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")  # auto would pick the Claude API
    monkeypatch.delenv("REINVENT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("REINVENT_MODEL", raising=False)
    try:
        config.save_preferences({"provider": "bedrock", "model": "claude-opus-4-7"})
        cfg = config.settings()
        assert (cfg.llm_provider, cfg.model) == ("bedrock", "anthropic.claude-opus-4-7")
        config.save_preferences({"provider": "anthropic"})
        assert config.settings().model == "claude-opus-4-8"
        monkeypatch.setenv("REINVENT_LLM_PROVIDER", "bedrock")  # env still wins
        config.settings.cache_clear()
        assert config.settings().llm_provider == "bedrock"
        config.save_preferences({"provider": "auto"})
        monkeypatch.delenv("REINVENT_LLM_PROVIDER")
        assert config.settings().llm_provider == "anthropic"
    finally:
        config.settings.cache_clear()


def test_provider_model_ids():
    from reinvent_agent.llm import get_provider

    assert get_provider("bedrock").model_id("claude-haiku-4-5") == "anthropic.claude-haiku-4-5"
    assert get_provider("anthropic").model_id("anthropic.claude-opus-4-8") == "claude-opus-4-8"
    import pytest

    with pytest.raises(ValueError):
        get_provider("openai")
