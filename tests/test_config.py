from feeder_agents.agent import configured_model


def test_dotenv_configures_qwen_without_network(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ('FEEDER_MODEL', 'OPENAI_API_KEY', 'OPENAI_BASE_URL'):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / '.env').write_text(
        'FEEDER_MODEL=qwen-plus\nOPENAI_API_KEY=test-only-key\n'
        'OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1\n')
    model = configured_model()
    assert model.model_name == 'qwen-plus'
    assert model.openai_api_base == 'https://dashscope.aliyuncs.com/compatible-mode/v1'
    assert model.openai_api_key.get_secret_value() == 'test-only-key'


def test_explicit_environment_takes_precedence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / '.env').write_text('FEEDER_MODEL=qwen-plus\nOPENAI_API_KEY=file-key\n')
    monkeypatch.setenv('FEEDER_MODEL', 'qwen-turbo')
    monkeypatch.setenv('OPENAI_API_KEY', 'environment-key')
    monkeypatch.setenv('OPENAI_BASE_URL', 'http://localhost:8000/v1')
    model = configured_model()
    assert model.model_name == 'qwen-turbo'
    assert model.openai_api_key.get_secret_value() == 'environment-key'
