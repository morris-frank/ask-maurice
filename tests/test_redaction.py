from __future__ import annotations

import logging

from ask_maurice.persona import PersonaBundle
from ask_maurice.runtime.redaction import PLACEHOLDER, Redactor, install


def test_verbatim_persona_content_is_detected_and_scrubbed(bundle: PersonaBundle):
    redactor = Redactor(bundle)
    leak = "As it happens, the widget dashboard rests on the data foundation, so we wait."

    assert redactor.leaks(leak)
    scrubbed = redactor.scrub(leak)
    assert "widget dashboard rests on the data foundation" not in scrubbed
    assert PLACEHOLDER in scrubbed
    assert scrubbed.startswith("As it happens,")


def test_ordinary_text_passes_through_untouched(bundle: PersonaBundle):
    redactor = Redactor(bundle)
    clean = "Rarefying by sequencing depth happens before the percentile comparison."
    assert not redactor.leaks(clean)
    assert redactor.scrub(clean) == clean


def test_short_text_is_never_flagged(bundle: PersonaBundle):
    redactor = Redactor(bundle)
    assert not redactor.leaks("the widget dashboard")


def test_log_records_are_scrubbed_at_the_handler(bundle: PersonaBundle, caplog):
    logger = logging.getLogger("test_redaction_target")
    logger.handlers.clear()
    logger.propagate = False
    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    logger.addHandler(Capture())
    install(Redactor(bundle), logger)

    logger.warning("debug dump: %s", "the widget dashboard rests on the data foundation now")
    assert records
    assert "rests on the data foundation" not in records[0]
    assert PLACEHOLDER in records[0]
