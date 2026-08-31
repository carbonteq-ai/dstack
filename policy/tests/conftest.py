import sys
from pathlib import Path

import pytest

# ctpolicy is built into the server image as its own wheel and is not a
# dependency of the dstack package, so it is not installed in the repo venv.
# Putting its source on the path keeps `uv run pytest policy/tests` working
# without adding ctpolicy to the root pyproject.toml, which is an upstream file.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ctpolicy import config as policy_config  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_policy_cache():
    """The loader caches by path+mtime; tests write files fast enough to collide."""
    policy_config.clear_cache()
    yield
    policy_config.clear_cache()


@pytest.fixture
def write_policy(tmp_path, monkeypatch):
    """Write a policy.yaml and point the loader's env var at it."""

    def _write(text: str) -> Path:
        path = tmp_path / "policy.yaml"
        path.write_text(text)
        monkeypatch.setenv(policy_config.POLICY_FILE_ENV_VAR, str(path))
        policy_config.clear_cache()
        return path

    return _write
