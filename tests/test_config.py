"""Misconfiguration is an error here, never a silent downgrade."""

from __future__ import annotations

import pytest

from ask_maurice.config import ConfigError, MixedbreadConfig

VARS = (
    "MXBAI_API_KEY",
    "ASK_MAURICE_LITERATURE_STORE",
    "ASK_MAURICE_VAULT_STORE",
    "ASK_MAURICE_VAULT_RETRIEVAL",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch):
    for name in VARS:
        monkeypatch.delenv(name, raising=False)


def test_no_key_and_no_stores_means_the_integration_is_simply_off():
    assert MixedbreadConfig.from_env() is None


def test_a_store_without_a_key_fails_loudly(monkeypatch: pytest.MonkeyPatch):
    """Otherwise the literature path is configured, dead, and looks configured."""
    monkeypatch.setenv("ASK_MAURICE_LITERATURE_STORE", "soilytix-papers")
    with pytest.raises(ConfigError, match="MXBAI_API_KEY"):
        MixedbreadConfig.from_env()


def test_semantic_vault_retrieval_without_an_indexed_store_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("MXBAI_API_KEY", "key")
    monkeypatch.setenv("ASK_MAURICE_VAULT_RETRIEVAL", "hybrid")
    with pytest.raises(ConfigError, match="ASK_MAURICE_VAULT_STORE"):
        MixedbreadConfig.from_env()


def test_an_unknown_retrieval_mode_is_rejected(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MXBAI_API_KEY", "key")
    monkeypatch.setenv("ASK_MAURICE_VAULT_RETRIEVAL", "embeddings-please")
    with pytest.raises(ConfigError, match="ASK_MAURICE_VAULT_RETRIEVAL"):
        MixedbreadConfig.from_env()


def test_literature_alone_leaves_vault_retrieval_local(monkeypatch: pytest.MonkeyPatch):
    """Wiring the papers store must not quietly move vault retrieval off disk."""
    monkeypatch.setenv("MXBAI_API_KEY", "key")
    monkeypatch.setenv("ASK_MAURICE_LITERATURE_STORE", "soilytix-papers")
    config = MixedbreadConfig.from_env()
    assert config is not None
    assert config.literature_enabled
    assert not config.vault_store_enabled
    assert config.vault_retrieval == "local"
