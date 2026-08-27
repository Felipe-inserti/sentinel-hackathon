"""Testes da integracao orchestrator.py <-> brand_agent.py (Sprint 7,
Parte A). `brand_agent.discover_brand_agent` em si ja e coberto em
`tests/test_brand_agent.py` -- aqui o foco e o comportamento do
orquestrador AO REDOR dela: uma marca sem BrandAgent publicado deve manter
o comportamento identico ao anterior a este sprint (nunca falhar a
investigacao), e uma marca com BrandAgent deve ter seu limiar de
escalonamento aplicado e `brand_agent_id`/`brand_agent_version`
carimbados."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import brand_agent as ba
import plane2_agents.orchestrator as orch
import registry


def _agent_manifest(version: str = "1.0.0") -> registry.AgentManifest:
    return registry.AgentManifest(
        agent_id="orchestrator",
        version=version,
        owner_team="sentinel-investigation",
        description="teste",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        tools_allowed=[],
        required_permissions=[],
        sla_seconds=10.0,
        status=registry.AgentStatus.ACTIVE,
        created_at=datetime.now(timezone.utc),
    )


def _brand_manifest() -> registry.AgentManifest:
    return registry.AgentManifest(
        agent_id="brand-agent-nubank",
        version="1.0.0",
        owner_team="sentinel-brand-ops",
        description="teste",
        input_schema=ba.BrandRoutingRequest.model_json_schema(),
        output_schema=ba.BrandGuidance.model_json_schema(),
        tools_allowed=["firestore.read"],
        required_permissions=["roles/datastore.user"],
        sla_seconds=2.0,
        status=registry.AgentStatus.ACTIVE,
        created_at=datetime.now(timezone.utc),
    )


def _brand_context(threshold: float = 0.85) -> ba.BrandContext:
    now = datetime.now(timezone.utc)
    return ba.BrandContext(
        brand_id="nubank",
        display_name="Nubank",
        legitimate_domains=["nubank.com.br"],
        risk_tolerance="LOW",
        confidence_escalation_threshold=threshold,
        created_at=now,
        updated_at=now,
    )


def _result(classification: str = "MALICIOUS", confidence: float = 0.8) -> orch.AnalysisResult:
    return orch.AnalysisResult(classification=classification, confidence=confidence, reasoning="teste")


def _usage() -> orch.LLMUsage:
    return orch.LLMUsage(model_id="gemini-3.6-flash", input_tokens=10, output_tokens=5, latency_ms=100.0)


def _sanitized(injection: list[str] | None = None) -> orch.SanitizationResult:
    return orch.SanitizationResult(
        clean_text="ok", injection_patterns_found=injection or [], pii_redacted={}
    )


# --- _save_investigation: brand=None preserva comportamento pre-Sprint-7 --


def test_save_investigation_without_brand_agent_keeps_previous_behavior(monkeypatch):
    fake_db = MagicMock()
    monkeypatch.setattr(orch, "db", fake_db)

    requires_review = orch._save_investigation(
        "dominio-teste.com", "marca-sem-agente", _result(confidence=0.99), _usage(), _sanitized(), 0.0001, _agent_manifest()
    )

    assert requires_review is False
    saved = fake_db.collection.return_value.document.return_value.set.call_args[0][0]
    assert saved["brand_agent_id"] is None
    assert saved["brand_agent_version"] is None


# --- _save_investigation: brand presente aplica limiar proprio -----------


def test_save_investigation_escalates_via_brand_threshold_even_without_injection(monkeypatch):
    fake_db = MagicMock()
    monkeypatch.setattr(orch, "db", fake_db)

    brand = ba.BrandAgent(context=_brand_context(threshold=0.9), agent_manifest=_brand_manifest())
    # confidence 0.8 < limiar 0.9 da marca, MALICIOUS, sem sinal de injecao
    requires_review = orch._save_investigation(
        "nubank-fake.com", "nubank", _result(classification="MALICIOUS", confidence=0.8),
        _usage(), _sanitized(), 0.0001, _agent_manifest(), brand,
    )

    assert requires_review is True
    saved = fake_db.collection.return_value.document.return_value.set.call_args[0][0]
    assert saved["brand_agent_id"] == "brand-agent-nubank"
    assert saved["brand_agent_version"] == "1.0.0"


def test_save_investigation_brand_threshold_met_does_not_force_review(monkeypatch):
    fake_db = MagicMock()
    monkeypatch.setattr(orch, "db", fake_db)

    brand = ba.BrandAgent(context=_brand_context(threshold=0.5), agent_manifest=_brand_manifest())
    requires_review = orch._save_investigation(
        "nubank-fake.com", "nubank", _result(classification="MALICIOUS", confidence=0.95),
        _usage(), _sanitized(), 0.0001, _agent_manifest(), brand,
    )

    assert requires_review is False


def test_save_investigation_injection_signal_still_forces_review_regardless_of_brand(monkeypatch):
    fake_db = MagicMock()
    monkeypatch.setattr(orch, "db", fake_db)

    brand = ba.BrandAgent(context=_brand_context(threshold=0.1), agent_manifest=_brand_manifest())
    requires_review = orch._save_investigation(
        "nubank-fake.com", "nubank", _result(classification="SAFE", confidence=0.99),
        _usage(), _sanitized(injection=["ignore_previous_instructions"]), 0.0001, _agent_manifest(), brand,
    )

    assert requires_review is True


# --- investigate_domain: resolve o BrandAgent quando ha matched_brand ----


@pytest.mark.asyncio
async def test_investigate_domain_resolves_brand_agent_on_cache_miss(monkeypatch):
    monkeypatch.setattr(orch, "_get_cached_investigation", lambda domain: None)
    monkeypatch.setattr(orch.telemetry, "flush_metrics_to_firestore", lambda *a, **k: None)
    monkeypatch.setattr(orch.telemetry, "increment_counter", lambda *a, **k: None)
    monkeypatch.setattr(orch, "_publish_completed", lambda *a, **k: None)

    fake_brand = object()

    async def _fake_classify(domain, matched_brand, few_shot_examples=None):
        return _result(), _usage(), _sanitized(), 0.0001, orch.BrandMemoryUsage(0, 0, 0.0)

    monkeypatch.setattr(orch, "classify_domain_with_gemini", _fake_classify)
    fake_discover = MagicMock(return_value=fake_brand)
    monkeypatch.setattr(orch.brand_agent, "discover_brand_agent", fake_discover)
    monkeypatch.setattr(orch.brand_memory, "get_relevant_memories", MagicMock(return_value=[]))

    captured = {}

    def _fake_save(domain, matched_brand, result, usage, sanitized, cost_usd, manifest, brand=None, detected_at=None):
        captured["brand"] = brand
        return False

    monkeypatch.setattr(orch, "_save_investigation", _fake_save)

    await orch.investigate_domain("nubank-fake.com", "nubank", _agent_manifest())

    fake_discover.assert_called_once_with("nubank", "nubank-fake.com")
    assert captured["brand"] is fake_brand


@pytest.mark.asyncio
async def test_investigate_domain_skips_brand_discovery_without_matched_brand(monkeypatch):
    monkeypatch.setattr(orch, "_get_cached_investigation", lambda domain: None)
    monkeypatch.setattr(orch.telemetry, "flush_metrics_to_firestore", lambda *a, **k: None)
    monkeypatch.setattr(orch.telemetry, "increment_counter", lambda *a, **k: None)
    monkeypatch.setattr(orch, "_publish_completed", lambda *a, **k: None)

    async def _fake_classify(domain, matched_brand, few_shot_examples=None):
        return _result(classification="SAFE"), _usage(), _sanitized(), 0.0001, orch.BrandMemoryUsage(0, 0, 0.0)

    monkeypatch.setattr(orch, "classify_domain_with_gemini", _fake_classify)
    fake_discover = MagicMock()
    monkeypatch.setattr(orch.brand_agent, "discover_brand_agent", fake_discover)
    fake_get_memories = MagicMock()
    monkeypatch.setattr(orch.brand_memory, "get_relevant_memories", fake_get_memories)
    monkeypatch.setattr(orch, "_save_investigation", lambda *a, **k: False)

    await orch.investigate_domain("dominio-sem-marca.com", None, _agent_manifest())

    fake_discover.assert_not_called()
    fake_get_memories.assert_not_called()
