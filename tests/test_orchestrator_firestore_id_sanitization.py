"""Teste de regressao -- Sprint 2, Stage D (ver FINDINGS.md item 18).

Achado real: `domain` usado cru como ID de documento Firestore
(`.document(domain)`) quebra com `ValueError: A document must have an
even number of path elements` quando `domain` contem "/" -- Firestore
trata "/" como separador de caminho, nao como parte literal do ID. Em
producao real `domain` e sempre um hostname puro (CN/SAN de certificado),
entao isso nunca apareceu antes -- mas nada no schema garantia isso.

`_firestore_safe_document_id` corrige isso com substituicao deterministica
(nunca hash -- mantem o ID legivel). O valor ORIGINAL de `domain` continua
gravado no campo `domain` do documento, sem sanitizacao -- nada se perde."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import plane2_agents.orchestrator as orch


# --- _firestore_safe_document_id: unidade ----------------------------------


def test_bare_hostname_passes_through_unchanged():
    """Caso comum (99.9%% dos casos reais): hostname puro, sem alteracao
    nenhuma -- byte a byte."""
    assert orch._firestore_safe_document_id("banco-teste-fake.com") == "banco-teste-fake.com"


def test_domain_with_slash_is_escaped():
    raw = "seu-id-unico-sentinel-demo-target.storage.googleapis.com/index.html"
    safe = orch._firestore_safe_document_id(raw)
    assert "/" not in safe
    assert safe == "seu-id-unico-sentinel-demo-target.storage.googleapis.com%2Findex.html"


def test_domain_with_multiple_slashes_all_escaped():
    safe = orch._firestore_safe_document_id("a/b/c")
    assert "/" not in safe
    assert safe == "a%2Fb%2Fc"


def test_single_dot_is_escaped():
    """Firestore proibe ID igual a exatamente "." ou "..", sozinho."""
    assert orch._firestore_safe_document_id(".") != "."
    assert "/" not in orch._firestore_safe_document_id(".")


def test_double_dot_is_escaped():
    assert orch._firestore_safe_document_id("..") != ".."


def test_reserved_dunder_pattern_is_escaped():
    """Firestore reserva IDs que batem o padrao __.*__."""
    safe = orch._firestore_safe_document_id("__reserved__")
    assert not safe.startswith("__") or safe != "__reserved__"


def test_overlong_id_is_truncated_to_1500_bytes():
    raw = "a" * 2000
    safe = orch._firestore_safe_document_id(raw)
    assert len(safe.encode("utf-8")) <= 1500


# --- Integracao: _get_cached_investigation / _save_investigation -----------


def test_get_cached_investigation_uses_sanitized_id_for_domain_with_slash(monkeypatch):
    fake_db = MagicMock()
    monkeypatch.setattr(orch, "db", fake_db)
    fake_db.collection.return_value.document.return_value.get.return_value = MagicMock(exists=False)

    domain_with_path = "bucket.storage.googleapis.com/index.html"
    result = orch._get_cached_investigation(domain_with_path)

    assert result is None
    document_call_arg = fake_db.collection.return_value.document.call_args[0][0]
    assert "/" not in document_call_arg
    assert document_call_arg == orch._firestore_safe_document_id(domain_with_path)


def test_save_investigation_uses_sanitized_id_but_preserves_raw_domain_in_body(monkeypatch):
    """O ID do documento e sanitizado -- mas o CAMPO 'domain' dentro do
    corpo do documento continua com o valor original, cru, sem
    alteracao. Nada se perde, so o ID muda."""
    fake_db = MagicMock()
    monkeypatch.setattr(orch, "db", fake_db)

    manifest = MagicMock(agent_id="orchestrator", version="1.0.0")
    result = orch.AnalysisResult(classification="SAFE", confidence=0.9, reasoning="ok")
    usage = orch.LLMUsage(model_id="gemini-teste", input_tokens=10, output_tokens=5, latency_ms=1.0)
    sanitized = orch.SanitizationResult(clean_text="ok", injection_patterns_found=[], pii_redacted={})

    domain_with_path = "bucket.storage.googleapis.com/index.html"
    orch._save_investigation(domain_with_path, None, result, usage, sanitized, 0.0001, manifest)

    document_call_arg = fake_db.collection.return_value.document.call_args[0][0]
    assert "/" not in document_call_arg

    saved_body = fake_db.collection.return_value.document.return_value.set.call_args[0][0]
    assert saved_body["domain"] == domain_with_path  # valor CRU, original, preservado


@pytest.mark.parametrize("bare_domain", ["banco-teste-fake.com", "nubank-fake.com.br", "x.test"])
def test_bare_hostnames_never_change_document_id(monkeypatch, bare_domain):
    """Garante que dominios reais (sem "/") continuam gravando no MESMO ID
    de sempre -- ninguem que ja tem cache hoje perde o cache com esta
    correcao."""
    fake_db = MagicMock()
    monkeypatch.setattr(orch, "db", fake_db)
    fake_db.collection.return_value.document.return_value.get.return_value = MagicMock(exists=False)

    orch._get_cached_investigation(bare_domain)

    document_call_arg = fake_db.collection.return_value.document.call_args[0][0]
    assert document_call_arg == bare_domain
