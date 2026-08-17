"""How a slash-command answer reaches the channel, and what stays private.

The two things pinned here are a disclosure decision and a legibility one, which
is why they get a file rather than riding along in test_slack.py:

  - answers are `in_channel`, failures are `ephemeral`
  - a public answer quotes the question, because an ephemeral ack means Slack
    never posted the invocation
"""

from __future__ import annotations

import pytest

from ask_maurice.runtime.agent import AgentError, Answer
from ask_maurice.runtime.artifacts import ArtifactKind, Suggestion
from ask_maurice.runtime.corpus import CorpusError
from ask_maurice.runtime.server import slack_answer_text, slack_reply

NO_ARTIFACT = Suggestion(kind=ArtifactKind.NONE, reason="", available=False)
QUESTION = "why do we normalise by sequencing depth?"
SOURCE = "eng/benchmark-normalisation.md@abc12345"


def _answer(text: str = "Because depth varies.", suggestion: Suggestion = NO_ARTIFACT) -> Answer:
    return Answer(
        text=text,
        caller="ada@testco.com",
        suggestion=suggestion,
        sources=[SOURCE],
    )


# --- the disclosure decision -------------------------------------------------


def test_an_answer_goes_to_the_channel():
    """The whole point of the change: the team sees each other's answers."""
    text, response_type = slack_reply(_answer, QUESTION, "ada@testco.com")
    assert response_type == "in_channel"
    assert "Because depth varies." in text


@pytest.mark.parametrize(
    "exc",
    [
        AgentError("rate limited by the Anthropic API; retry shortly"),
        CorpusError("corpus has no COMMIT file"),
    ],
)
def test_a_known_failure_stays_private(exc: Exception):
    """Nobody but the person waiting needs to read a retry notice."""

    def boom() -> Answer:
        raise exc

    text, response_type = slack_reply(boom, QUESTION, "ada@testco.com")
    assert response_type == "ephemeral"
    assert text.startswith(":warning:")
    assert str(exc) in text


def test_an_unexpected_failure_stays_private_and_says_nothing_useful_to_an_attacker():
    def boom() -> Answer:
        raise RuntimeError("psycopg2.OperationalError: password authentication failed for maurice")

    text, response_type = slack_reply(boom, QUESTION, "ada@testco.com")
    assert response_type == "ephemeral"
    assert "password" not in text
    assert "logs" in text


def test_nothing_escapes_into_a_dead_request():
    """The ack went out seconds ago. An exception here reaches nobody, and the
    asker gets silence — indistinguishable from a broken integration."""

    def boom() -> Answer:
        raise BaseExceptionGroup("nested", [RuntimeError("a"), RuntimeError("b")])

    text, response_type = slack_reply(boom, QUESTION, "ada@testco.com")
    assert response_type == "ephemeral"
    assert text.startswith(":warning:")


# --- legibility in the channel ------------------------------------------------


def test_a_public_answer_quotes_the_question():
    """An ephemeral ack means Slack never echoed the invocation, so without this
    the answer lands in the channel with nothing saying what was asked."""
    text = slack_answer_text(_answer(), question=QUESTION)
    assert text.startswith(f"> {QUESTION}")
    assert text.index(QUESTION) < text.index("Because depth varies.")


def test_the_quote_is_omitted_when_there_is_no_question():
    text = slack_answer_text(_answer())
    assert not text.startswith(">")
    assert text.startswith("Because depth varies.")


def test_sources_and_artifact_hints_survive_the_echo():
    answer = _answer(
        suggestion=Suggestion(kind=ArtifactKind.DOCUMENT, reason="explainer", available=False)
    )
    text = slack_answer_text(answer, question=QUESTION)
    assert SOURCE in text
    assert "document" in text
    assert "not wired up yet" in text
