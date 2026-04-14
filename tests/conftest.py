"""Shared fixtures for axor-claude tests."""
from __future__ import annotations
import sys, os

# ensure axor-core is importable when running tests from axor-claude dir
_axor_core_path = os.path.join(os.path.dirname(__file__), "..", "..", "axor-core")
if os.path.exists(_axor_core_path):
    sys.path.insert(0, _axor_core_path)

import pytest
from axor_claude.events import StreamNormalizer


class MockSdkEvent:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class MockUsage:
    def __init__(self, input_tokens=0, output_tokens=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


@pytest.fixture
def node_id():
    return "node_test_001"


@pytest.fixture
def normalizer(node_id):
    return StreamNormalizer(node_id=node_id)


@pytest.fixture
def mock_event():
    return MockSdkEvent


@pytest.fixture
def mock_usage():
    return MockUsage
