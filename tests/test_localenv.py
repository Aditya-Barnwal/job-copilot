"""CLI entry points load .env like the server (the nightly run silently used the default LLM before)."""
import os

from jobcopilot import localenv


def test_loads_a_dotenv_when_allowed(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("JC_TEST_FAKE_PROVIDER=groq\n")
    monkeypatch.setattr(localenv, "ROOT", tmp_path)
    monkeypatch.delenv("JOBCOPILOT_NO_DOTENV", raising=False)
    monkeypatch.delenv("JOBCOPILOT_DEMO", raising=False)
    monkeypatch.delenv("JC_TEST_FAKE_PROVIDER", raising=False)
    assert localenv.load() is True
    assert os.environ.pop("JC_TEST_FAKE_PROVIDER") == "groq"


def test_never_in_tests_or_demo_and_env_wins(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("JC_TEST_FAKE_PROVIDER=groq\n")
    monkeypatch.setattr(localenv, "ROOT", tmp_path)
    monkeypatch.setenv("JOBCOPILOT_NO_DOTENV", "1")
    assert localenv.load() is False and "JC_TEST_FAKE_PROVIDER" not in os.environ
    monkeypatch.delenv("JOBCOPILOT_NO_DOTENV")
    monkeypatch.setenv("JOBCOPILOT_DEMO", "1")
    assert localenv.load() is False
    monkeypatch.delenv("JOBCOPILOT_DEMO")
    monkeypatch.setenv("JC_TEST_FAKE_PROVIDER", "ollama")      # already set: the file never overrides it
    localenv.load()
    assert os.environ["JC_TEST_FAKE_PROVIDER"] == "ollama"


def test_daily_loads_env_before_importing_the_rest():
    src = (localenv.ROOT / "jobcopilot" / "daily.py").read_text()
    assert src.index("_localenv.load()") < src.index("from . import config")
