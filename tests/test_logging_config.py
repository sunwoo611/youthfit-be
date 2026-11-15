import logging
from collections.abc import Iterator

import pytest

from app.core.logging_config import APP_LOGGER, configure_logging


@pytest.fixture
def app_logger(monkeypatch: pytest.MonkeyPatch) -> Iterator[logging.Logger]:
    """`app` 로거를 설정 전 상태로 두고, 끝나면 원래 상태로 되돌린다."""
    logger = logging.getLogger(APP_LOGGER)
    level = logger.level
    monkeypatch.setattr(logger, "handlers", [])
    logger.setLevel(logging.NOTSET)
    yield logger
    logger.setLevel(level)


def test_adds_stderr_handler_when_nothing_is_configured(
    app_logger: logging.Logger, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(logging.getLogger(), "handlers", [])

    configure_logging()
    configure_logging()

    assert len(app_logger.handlers) == 1
    assert logging.getLogger("app.llm.client").isEnabledFor(logging.INFO)


def test_keeps_existing_handlers_and_level(app_logger: logging.Logger):
    existing = logging.NullHandler()
    app_logger.addHandler(existing)
    app_logger.setLevel(logging.WARNING)

    configure_logging()

    assert app_logger.handlers == [existing]
    assert app_logger.level == logging.WARNING


def test_uses_root_handlers_without_adding_one(
    app_logger: logging.Logger, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(logging.getLogger(), "handlers", [logging.NullHandler()])

    configure_logging()

    assert app_logger.handlers == []
    assert app_logger.level == logging.INFO
