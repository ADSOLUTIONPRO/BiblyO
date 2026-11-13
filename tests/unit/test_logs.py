# SPDX-License-Identifier: EUPL-1.2
from __future__ import annotations

import logging

import pytest

from biblyo.core.logs import HANDLER_NAME, StructuredFormatter, configure_logging


@pytest.fixture
def clean_logger():
    logger = logging.getLogger("biblyo")
    before = list(logger.handlers), logger.level
    logger.handlers = [h for h in logger.handlers if h.get_name() != HANDLER_NAME]
    yield logger
    logger.handlers, logger.level = list(before[0]), before[1]


def test_format_has_fixed_fields_then_the_message():
    record = logging.LogRecord("biblyo.essai", logging.WARNING, __file__, 1, "Un message", None, None)
    record.created = 0
    line = StructuredFormatter().format(record)
    assert line == "at=1970-01-01T00:00:00Z level=WARNING logger=biblyo.essai Un message"


def test_configuration_is_idempotent_and_applies_the_level(clean_logger):
    configure_logging("debug")
    configure_logging("warning")
    assert [h.get_name() for h in clean_logger.handlers].count(HANDLER_NAME) == 1
    assert clean_logger.level == logging.WARNING
