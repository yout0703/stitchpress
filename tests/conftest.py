import pytest

from stitchpress.core.models import ThreadColor, new_design


@pytest.fixture
def minimal():
    colors = [
        ThreadColor("DMC", "001", "Test black", (0, 0, 0), "test-v1"),
        ThreadColor("DMC", "3865", "White subject", (255, 255, 255), "test-v1"),
    ]
    return new_design(3, 2, colors, [[0, -1, 1], [1, 0, 0]])
