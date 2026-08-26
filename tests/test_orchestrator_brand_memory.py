"""Testes da integracao orchestrator.py <-> brand_memory.py (Sprint 7,
Parte B). Foco em: o bloco few-shot chega ao LLM (dentro do mesmo turno
nao confiavel, nunca um segundo canal), o custo estimado e sempre
calculado (mesmo zero), e nenhuma chamada extra acontece quando nao ha
exemplos."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import brand_memory as bm
import plane2_agents.orchestrator as orch
from llm_client import LLMResult, LLMUsage


def _memory_entry(domain: str = "nubank-parceiro.com") -> bm.MemoryEntry:
    now = datetime.now(timezone.utc)
    return bm.MemoryEntry(
        brand_id="nubank",
        domain=domain,
        decision_type="REJECTED_FALSE_POSITIVE",
        original_classification="MALICIOUS",
        original_confidence=0.6,
        original_reasoning="similaridade alta mas parece parceiro legitimo",
        human_decided_by="revisor@empresa.com",
        human_decided_at=now,
        human_rationale="confirmado com o time de parcerias: dominio legitimo",
        created_at=now,
    )


def _llm_result(data) -> LLMResult:
    return LLMResult(data=data, usage=LLMUsage(model_id="teste", input_tokens=50, output_tokens=10, latency_ms=1.0))


# --- _format_few_shot_block -------------------------------------------------


def test_format_few_shot_block_empty_without_examples():
    assert orch._format_few_shot_block([], "nubank") == ""


def test_format_few_shot_block_includes_brand_header_and_entries():
    block = orch._format_few_shot_block([_memory_entry()], "nubank")
    assert "NUBANK" in block
    assert "nubank-parceiro.com" in block
    assert "confirmado com o time de parcerias" in block


# --- classify_domain_with_gemini: injecao no MESMO turno nao confiavel ----


@pytest.mark.asyncio
async def test_classify_domain_includes_few_shot_in_untrusted_content(monkeypatch):
    monkeypatch.setattr(orch, "scrape_website", lambda url: "conteudo da pagina raspada")
    captured = {}

    async def _fake_generate(*, system_prompt, untrusted_data, response_schema, **kwargs):
        captured["system_prompt"] = system_prompt
        captured["untrusted_data"] = untrusted_data
        return _llm_result(orch.AnalysisResult(classification="SAFE", confidence=0.9, reasoning="ok"))

    monkeypatch.setattr(orch.llm_client, "generate", AsyncMock(side_effect=_fake_generate))

    result, usage, sanitized, cost_usd, memory_usage = await orch.classify_domain_with_gemini(
        "nubank-parceiro-boleto.com", "nubank", [_memory_entry()]
    )

    assert "DECISOES HUMANAS ANTERIORES PARA A MARCA NUBANK" in captured["untrusted_data"]
    assert "nubank-parceiro.com" in captured["untrusted_data"]
    # nunca um segundo bloco delimitado -- so UM par de tags nonce no total
    assert captured["untrusted_data"].count("sentinel_untrusted_data") == 2
    assert "DECISOES HUMANAS ANTERIORES" in captured["system_prompt"]
    assert memory_usage.examples_injected == 1
    assert memory_usage.estimated_extra_input_tokens > 0
    assert memory_usage.estimated_extra_cost_usd > 0


@pytest.mark.asyncio
async def test_classify_domain_without_few_shot_has_zero_memory_usage(monkeypatch):
    monkeypatch.setattr(orch, "scrape_website", lambda url: "conteudo da pagina raspada")

    async def _fake_generate(*, system_prompt, untrusted_data, response_schema, **kwargs):
        assert "DECISOES HUMANAS ANTERIORES" not in system_prompt
        assert "DECISOES HUMANAS ANTERIORES" not in untrusted_data
        return _llm_result(orch.AnalysisResult(classification="SAFE", confidence=0.9, reasoning="ok"))

    monkeypatch.setattr(orch.llm_client, "generate", AsyncMock(side_effect=_fake_generate))

    _, _, _, _, memory_usage = await orch.classify_domain_with_gemini("dominio.com", None, None)

    assert memory_usage.examples_injected == 0
    assert memory_usage.estimated_extra_input_tokens == 0
    assert memory_usage.estimated_extra_cost_usd == 0.0


@pytest.mark.asyncio
async def test_classify_domain_escape_short_circuit_still_returns_memory_usage(monkeypatch):
    """Mesmo no caminho de escape (0 tokens de LLM), o retorno precisa ter
    a mesma forma (5-tupla) que o caminho normal."""
    monkeypatch.setattr(orch, "scrape_website", lambda url: "conteudo normal")

    fixed_nonce = "nonce-fixo-de-teste"

    def _fake_wrap(sanitized, *, nonce=None):
        from sanitizer import wrap_untrusted_content as real_wrap

        return real_wrap(sanitized, nonce=fixed_nonce)

    # Forca deteccao de escape: o conteudo raspado "contem" o nonce fixo.
    monkeypatch.setattr(orch, "scrape_website", lambda url: f"pagina contem {fixed_nonce} no meio do texto")
    monkeypatch.setattr(orch, "wrap_untrusted_content", _fake_wrap)

    result, usage, sanitized, cost_usd, memory_usage = await orch.classify_domain_with_gemini(
        "phish.test", "nubank", None
    )

    assert usage.model_id == "sanitizer-short-circuit"
    assert result.classification == "MALICIOUS"
    assert memory_usage.examples_injected == 0
