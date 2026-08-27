"""Testes da Etapa C, item 1 -- guarda de custo do run de observacao,
integrada em `classify_domain_with_gemini`/`investigate_domain`
(plane2_agents/orchestrator.py). `observation_run.cost_guard_allows_llm_call`
em si ja e coberto exaustivamente em tests/test_observation_run.py -- aqui
o foco e o comportamento do orquestrador AO REDOR dela: uma recusa nunca
chama `llm_client.generate` (0 tokens gastos), sempre forca revisao humana,
e nunca conta como uma invocacao real de LLM nos contadores."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import plane2_agents.orchestrator as orch


def _sanitized_ok():
    return orch.sanitize("conteudo normal, sem tentativa de escape")


@pytest.mark.asyncio
async def test_classify_domain_blocked_by_cost_guard_never_calls_llm(monkeypatch):
    monkeypatch.setattr(orch, "scrape_website", lambda url: "conteudo da pagina raspada")
    monkeypatch.setattr(orch.settings, "observation_run_id", "obs-teste")
    monkeypatch.setattr(orch.settings, "observation_cost_guard_usd_limit", 10.0)
    monkeypatch.setattr(orch.observation_run, "cost_guard_allows_llm_call", lambda: False)
    fake_generate = AsyncMock()
    monkeypatch.setattr(orch.llm_client, "generate", fake_generate)

    result, usage, sanitized, cost_usd, memory_usage = await orch.classify_domain_with_gemini(
        "dominio-suspeito.com", "nubank", None
    )

    fake_generate.assert_not_called()
    assert usage.model_id == "cost-guard-blocked"
    assert usage.input_tokens == 0 and usage.output_tokens == 0
    assert cost_usd == 0.0
    assert result.classification == "SAFE"
    assert "guarda de custo" in result.reasoning.lower() or "GUARDA" in result.reasoning


@pytest.mark.asyncio
async def test_classify_domain_not_blocked_when_guard_allows(monkeypatch):
    from llm_client import LLMResult, LLMUsage

    monkeypatch.setattr(orch, "scrape_website", lambda url: "conteudo da pagina raspada")
    monkeypatch.setattr(orch.observation_run, "cost_guard_allows_llm_call", lambda: True)

    async def _fake_generate(*, system_prompt, untrusted_data, response_schema, **kwargs):
        return LLMResult(
            data=orch.AnalysisResult(classification="SAFE", confidence=0.9, reasoning="ok"),
            usage=LLMUsage(model_id="gemini-teste", input_tokens=100, output_tokens=20, latency_ms=1.0),
        )

    monkeypatch.setattr(orch.llm_client, "generate", AsyncMock(side_effect=_fake_generate))

    result, usage, sanitized, cost_usd, memory_usage = await orch.classify_domain_with_gemini(
        "dominio-suspeito.com", "nubank", None
    )

    assert usage.model_id == "gemini-teste"
    assert cost_usd > 0.0


def test_save_investigation_forces_human_review_when_cost_guard_blocked(monkeypatch):
    from llm_client import LLMUsage

    result = orch.AnalysisResult(classification="SAFE", confidence=0.0, reasoning="bloqueado")
    usage = LLMUsage(model_id="cost-guard-blocked", input_tokens=0, output_tokens=0, latency_ms=0.0)
    manifest = MagicMock(agent_id="orchestrator", version="1.0.0")

    fake_db = MagicMock()
    monkeypatch.setattr(orch, "db", fake_db)

    requires_review = orch._save_investigation(
        "dominio.com", "nubank", result, usage, _sanitized_ok(), 0.0, manifest
    )

    assert requires_review is True


@pytest.mark.asyncio
async def test_investigate_domain_does_not_count_cost_guard_block_as_invocation(monkeypatch):
    monkeypatch.setattr(orch, "_get_cached_investigation", lambda domain: None)
    monkeypatch.setattr(orch, "_publish_completed", lambda *a, **k: None)
    monkeypatch.setattr(orch, "_save_investigation", lambda *a, **k: True)
    monkeypatch.setattr(orch.brand_agent, "discover_brand_agent", MagicMock(return_value=None))

    from llm_client import LLMUsage

    blocked_result = orch.AnalysisResult(classification="SAFE", confidence=0.0, reasoning="bloqueado")
    blocked_usage = LLMUsage(model_id="cost-guard-blocked", input_tokens=0, output_tokens=0, latency_ms=0.0)

    async def _fake_classify(domain, matched_brand, few_shot_examples=None):
        return blocked_result, blocked_usage, _sanitized_ok(), 0.0, orch.BrandMemoryUsage(0, 0, 0.0)

    monkeypatch.setattr(orch, "classify_domain_with_gemini", _fake_classify)

    fake_increment = MagicMock()
    monkeypatch.setattr(orch.telemetry, "increment_counter", fake_increment)
    monkeypatch.setattr(orch.telemetry, "flush_metrics_to_firestore", MagicMock())
    monkeypatch.setattr(orch.observation_run, "bump", MagicMock())

    manifest = MagicMock(agent_id="orchestrator", version="1.0.0")
    await orch.investigate_domain("dominio-bloqueado.com", "nubank", manifest)

    called_counters = {call.args[0] for call in fake_increment.call_args_list}
    assert "llm_invocations_total" not in called_counters
    assert "estimated_cost_usd_total" not in called_counters
