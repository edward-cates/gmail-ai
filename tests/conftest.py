"""Pytest configuration and shared fixtures."""

import sys
from unittest.mock import MagicMock

# Mock langchain modules if not available (for testing without full dependencies)
_mock_langchain = MagicMock()
_mock_langchain.language_models = MagicMock()
_mock_langchain.tools = MagicMock()
_mock_langchain.tools.StructuredTool = MagicMock()

sys.modules["langchain_core"] = _mock_langchain
sys.modules["langchain_core.language_models"] = _mock_langchain.language_models
sys.modules["langchain_core.tools"] = _mock_langchain.tools
sys.modules["langchain_anthropic"] = MagicMock()
sys.modules["langchain_google_genai"] = MagicMock()

# Mock Google Cloud modules if not available
_mock_google_cloud = MagicMock()
_mock_google_cloud.run_v2 = MagicMock()
_mock_google_cloud.tasks_v2 = MagicMock()

sys.modules["google.cloud"] = _mock_google_cloud
sys.modules["google.cloud.run_v2"] = _mock_google_cloud.run_v2
sys.modules["google.cloud.tasks_v2"] = _mock_google_cloud.tasks_v2

# Mock Flask for tests (if not installed)
try:
    import flask
except ImportError:
    _mock_flask = MagicMock()
    _mock_flask.Flask = MagicMock(return_value=MagicMock())
    _mock_flask.jsonify = MagicMock(return_value={})
    _mock_flask.request = MagicMock()
    sys.modules["flask"] = _mock_flask
