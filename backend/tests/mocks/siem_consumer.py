"""Loads the mock SIEM consumer from tools/agent so tests and the live loop
share one implementation (see tools/agent/mock_siem_consumer.py)."""
import importlib.util
import os

_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "tools", "agent", "mock_siem_consumer.py"))
_spec = importlib.util.spec_from_file_location("mock_siem_consumer", _PATH)
mock_siem_consumer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mock_siem_consumer)

MockSiem = mock_siem_consumer.MockSiem
