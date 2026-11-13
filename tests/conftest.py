import pytest

from stitchpress.core.models import ThreadColor, new_design


@pytest.fixture(autouse=True)
def finish_deferred_widget_cleanup(request):
    yield
    if "qtbot" in request.fixturenames:
        from PySide6.QtCore import QCoreApplication, QEvent

        # pytest-qt closes/deletes registered widgets in its teardown hook.
        # processEvents alone does not dispatch DeferredDelete without app.exec().
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def minimal():
    colors = [
        ThreadColor("DMC", "001", "Test black", (0, 0, 0), "test-v1"),
        ThreadColor("DMC", "3865", "White subject", (255, 255, 255), "test-v1"),
    ]
    return new_design(3, 2, colors, [[0, -1, 1], [1, 0, 0]])
