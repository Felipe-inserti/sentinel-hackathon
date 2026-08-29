"""Testes de `replay_investigation.py` -- Vertex AI (`llm_client.generate`)
e Firestore (`brand_memory`) sempre mockados: nenhuma chamada real ao
Gemini nem ao Firestore acontece aqui (isso exigiria GCP configurado, ver
REGRAS DA SESSAO no resumo do sprint). O que ESTES testes verificam,
executando de verdade:

  - a chamada "baseline" (sem memoria) NUNCA recebe o bloco few-shot no
    prompt, mesmo que existam memorias na marca;
  - a chamada "com memoria" recebe exatamente os exemplos devolvidos por
    `brand_memory.get_relevant_memories`;
  - a rejeicao do dossie e sempre gravada em brand_memory ANTES da
    comparacao (ordem correta: memoria precisa existir antes de ser lida);
  - `run_replay` devolve exit code 0 quando o LLM mockado de fato "corrige"
    (MALICIOUS sem memoria -> SAFE com memoria) e 1 quando nao corrige.

A alegacao "o Gemini REAL muda de veredito" e responsabilidade de rodar o
script de verdade (ver resumo do sprint) -- nao e, e nao pode ser, coberta
por um teste que mocka a propria chamada ao modelo."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import brand_memory as bm
import replay_investigation as ri
from llm_client import LLMResult, LLMUsage
from plane2_agents import orchestrator as orch


def _llm_result(data) -> LLMResult:
    return LLMResult(data=data, usage=LLMUsage(model_id="teste", input_tokens=40, output_tokens=8, latency_ms=1.0))


def _memory_entry(domain: str = ri.DEMO_DOMAIN) -> bm.MemoryEntry:
    now = datetime.now(timezone.utc)
    return bm.MemoryEntry(
        brand_id=ri.DEMO_BRAND_ID,
        domain=domain,
        decision_type="REJECTED_FALSE_POSITIVE",
        original_classification="MALICIOUS",
        original_confidence=0.63,
        original_reasoning="ok",
        human_decided_by=ri.DEMO_REJECTED_BY,
        human_decided_at=now,
        human_rationale=ri.DEMO_REJECTION_REASON,
        created_at=now,
    )


def _patch_common(monkeypatch, *, few_shot_examples):
    """Mocka record_rejection/get_relevant_memories -- garante que nenhuma
    chamada real ao Firestore acontece. Tambem mocka a captura de tela
    (Sprint multimodal, ver plane2_agents/page_capture.py) -- sem isso,
    `classify_domain_with_gemini` (chamada de verdade neste script, so
    scrape_website e substituido -- ver `_classify_with_fixed_content`)
    tentaria abrir um Chromium real contra um dominio fake/inexistente em
    CADA teste, so retornando (via timeout) depois de ~15s por chamada."""
    fake_record = MagicMock(return_value=_memory_entry())
    monkeypatch.setattr(ri.brand_memory, "record_rejection", fake_record)
    fake_get = MagicMock(return_value=few_shot_examples)
    monkeypatch.setattr(ri.brand_memory, "get_relevant_memories", fake_get)
    monkeypatch.setattr(orch.page_capture, "capture_page_screenshot", AsyncMock(return_value=None))
    return fake_record, fake_get


@pytest.mark.asyncio
async def test_demo_mode_baseline_call_never_includes_few_shot(monkeypatch, capsys):
    """Criterio central: a chamada SEM memoria nunca recebe o bloco
    few-shot, mesmo com memorias disponiveis para a marca."""
    _patch_common(monkeypatch, few_shot_examples=[_memory_entry()])

    calls: list[dict] = []

    async def _fake_generate(*, system_prompt, untrusted_data, response_schema, **kwargs):
        calls.append({"system_prompt": system_prompt, "untrusted_data": untrusted_data})
        # primeira chamada = baseline (sem memoria) -> repete o erro
        if len(calls) == 1:
            return _llm_result(orch.AnalysisResult(classification="MALICIOUS", confidence=0.6, reasoning="baseline"))
        return _llm_result(orch.AnalysisResult(classification="SAFE", confidence=0.9, reasoning="corrigido"))

    monkeypatch.setattr(orch.llm_client, "generate", AsyncMock(side_effect=_fake_generate))

    exit_code = await ri.run_replay(None, brand_override=None, limit=3)

    assert len(calls) == 2
    assert "DECISOES HUMANAS ANTERIORES" not in calls[0]["untrusted_data"]
    assert "DECISOES HUMANAS ANTERIORES" not in calls[0]["system_prompt"]
    assert "DECISOES HUMANAS ANTERIORES" in calls[1]["untrusted_data"]
    assert "DECISOES HUMANAS ANTERIORES" in calls[1]["system_prompt"]
    assert exit_code == 0

    out = capsys.readouterr().out
    assert "CORRIGIDO VIA MEMORIA" in out


@pytest.mark.asyncio
async def test_demo_mode_records_rejection_before_reading_memory(monkeypatch):
    fake_record, fake_get = _patch_common(monkeypatch, few_shot_examples=[])

    async def _fake_generate(*, system_prompt, untrusted_data, response_schema, **kwargs):
        return _llm_result(orch.AnalysisResult(classification="MALICIOUS", confidence=0.6, reasoning="x"))

    monkeypatch.setattr(orch.llm_client, "generate", AsyncMock(side_effect=_fake_generate))

    await ri.run_replay(None, brand_override=None, limit=3)

    fake_record.assert_called_once()
    kwargs = fake_record.call_args.kwargs
    assert kwargs["brand_id"] == ri.DEMO_BRAND_ID
    assert kwargs["domain"] == ri.DEMO_DOMAIN
    assert kwargs["investigation"]["rejection_reason"] == ri.DEMO_REJECTION_REASON
    fake_get.assert_called_once_with(ri.DEMO_BRAND_ID, ri.DEMO_DOMAIN, limit=3)


@pytest.mark.asyncio
async def test_demo_mode_both_calls_receive_identical_scraped_content(monkeypatch):
    """A UNICA variavel entre as duas chamadas deve ser a memoria -- o
    conteudo raspado (aqui, fixo) precisa ser identico."""
    _patch_common(monkeypatch, few_shot_examples=[_memory_entry()])

    captured_urls_content: list[str] = []

    async def _fake_generate(*, system_prompt, untrusted_data, response_schema, **kwargs):
        captured_urls_content.append(untrusted_data)
        return _llm_result(orch.AnalysisResult(classification="SAFE", confidence=0.9, reasoning="x"))

    monkeypatch.setattr(orch.llm_client, "generate", AsyncMock(side_effect=_fake_generate))

    await ri.run_replay(None, brand_override=None, limit=3)

    # O conteudo raspado (sanitizado) precisa aparecer em ambas as chamadas
    assert "XPTO" in captured_urls_content[0]
    assert "XPTO" in captured_urls_content[1]


@pytest.mark.asyncio
async def test_run_replay_returns_nonzero_when_not_corrected(monkeypatch):
    _patch_common(monkeypatch, few_shot_examples=[_memory_entry()])

    async def _fake_generate(*, system_prompt, untrusted_data, response_schema, **kwargs):
        # nunca corrige -- sempre MALICIOUS, com ou sem memoria
        return _llm_result(orch.AnalysisResult(classification="MALICIOUS", confidence=0.6, reasoning="x"))

    monkeypatch.setattr(orch.llm_client, "generate", AsyncMock(side_effect=_fake_generate))

    exit_code = await ri.run_replay(None, brand_override=None, limit=3)
    assert exit_code == 1


@pytest.mark.asyncio
async def test_real_mode_rejects_domain_without_rejected_status(monkeypatch):
    monkeypatch.setattr(
        orch, "_get_cached_investigation", lambda domain: {"status": "PENDING_HUMAN_REVIEW", "matched_brand": "nubank"}
    )

    with pytest.raises(SystemExit) as exc_info:
        await ri.run_replay("algum-dominio.com", brand_override=None, limit=3)
    assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_real_mode_rejects_missing_domain(monkeypatch):
    monkeypatch.setattr(orch, "_get_cached_investigation", lambda domain: None)

    with pytest.raises(SystemExit):
        await ri.run_replay("nunca-investigado.com", brand_override=None, limit=3)


@pytest.mark.asyncio
async def test_real_mode_uses_live_scrape_and_stored_dossier(monkeypatch):
    real_investigation = {
        "domain": "nubank-fake-real.com",
        "matched_brand": "nubank",
        "classification": "MALICIOUS",
        "confidence": 0.7,
        "reasoning": "ok",
        "status": "REJECTED",
        "rejected_by": "revisor@empresa.com",
        "rejected_at": "2026-08-15T10:00:00+00:00",
        "rejection_reason": "falso positivo confirmado",
    }
    monkeypatch.setattr(orch, "_get_cached_investigation", lambda domain: real_investigation)
    monkeypatch.setattr(orch, "scrape_website", lambda url: "conteudo re-raspado ao vivo")
    _patch_common(monkeypatch, few_shot_examples=[])

    async def _fake_generate(*, system_prompt, untrusted_data, response_schema, **kwargs):
        return _llm_result(orch.AnalysisResult(classification="SAFE", confidence=0.8, reasoning="x"))

    monkeypatch.setattr(orch.llm_client, "generate", AsyncMock(side_effect=_fake_generate))

    await ri.run_replay("nubank-fake-real.com", brand_override=None, limit=3)

    ri.brand_memory.record_rejection.assert_called_once()
    kwargs = ri.brand_memory.record_rejection.call_args.kwargs
    assert kwargs["domain"] == "nubank-fake-real.com"
    assert kwargs["brand_id"] == "nubank"
