"""Testes de integracao do batching+triagem em `ct_listener.py`.

Cobrem o roteamento DISCARD/INVESTIGATE/ESCALATE_IMMEDIATE e, principalmente,
o requisito de aceite explicito: "fail-open comprovado por teste que derruba
o servico do Gemma" -- aqui o Ollama e derrubado de verdade (via httpx),
nao so a funcao `triage_batch` mockada em alto nivel.

Entrada trocada do certstream (`handle_certstream_message`, removido) para
`ct_rfc6962.ParsedCertEntry` + `ctl._process_certificate_entry` (ver
sprint de polling RFC 6962) -- o pipeline downstream testado aqui
(prefiltro -> fila Gemma -> Pub/Sub) e EXATAMENTE o mesmo de antes."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import plane1_ingestion.ct_listener as ctl
import telemetry
from gemma_triage import TriageBatchOutcome, TriageResult
from plane1_ingestion.ct_rfc6962 import ParsedCertEntry


@pytest.fixture(autouse=True)
def _wire_mocks():
    ctl._publisher = MagicMock()
    published: list[dict] = []

    def fake_publish(topic, data, **kwargs):
        published.append(kwargs)
        future = MagicMock()
        future.exception.return_value = None
        return future

    ctl._publisher.publish.side_effect = fake_publish

    ctl._firestore_db = MagicMock()
    discards: list[dict] = []
    ctl._firestore_db.collection.return_value.document.return_value.set.side_effect = discards.append

    telemetry._metrics_db = MagicMock()
    ctl._pending_batch = []

    yield published, discards


def _parsed_entry(domains: list[str]) -> ParsedCertEntry:
    return ParsedCertEntry(
        log_index=1,
        entry_type="x509_entry",
        domains=domains,
        certificate_age_seconds=120.0,
    )


@pytest.mark.asyncio
async def test_gemma_verdicts_route_correctly(_wire_mocks):
    published, discards = _wire_mocks
    import asyncio

    async def fake_triage_batch(signals):
        results = {}
        for s in signals:
            if "discard" in s.domain:
                verdict, score = "DISCARD", 0.1
            elif "escalate" in s.domain:
                verdict, score = "ESCALATE_IMMEDIATE", 0.98
            else:
                verdict, score = "INVESTIGATE", 0.6
            results[s.domain] = TriageResult(
                domain=s.domain, verdict=verdict, risk_score=score,
                target_brand=s.target_brand, rationale="teste",
            )
        return TriageBatchOutcome(
            results=results, latency_ms=50.0, cost_usd=0.0001,
            fallback_used=False, model_id="gemma3:270m",
        )

    with patch("plane1_ingestion.ct_listener.triage_batch", fake_triage_batch):
        entry = _parsed_entry(
            [
                "nub4nk-discard-caso.xyz",
                "nub4nk-investigate-caso.xyz",
                "nub4nk-escalate-caso.xyz",
            ]
        )
        await ctl._process_certificate_entry(entry)
        await asyncio.sleep(0.3)
        await ctl._flush_triage_batch()

    assert len(published) == 2
    priorities = {p["domain"]: p["priority"] for p in published}
    assert priorities["nub4nk-investigate-caso.xyz"] == "INVESTIGATE"
    assert priorities["nub4nk-escalate-caso.xyz"] == "ESCALATE_IMMEDIATE"
    assert len(discards) == 1
    assert discards[0]["domain"] == "nub4nk-discard-caso.xyz"
    assert discards[0]["gemma_verdict"] == "DISCARD"


class _DownAsyncClient:
    """Substitui `httpx.AsyncClient` simulando o servico do Gemma
    totalmente fora do ar -- toda chamada levanta ConnectionError."""

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        raise ConnectionError("Gemma Cloud Run esta fora do ar")


@pytest.mark.asyncio
async def test_fail_open_when_gemma_service_is_down(_wire_mocks):
    """Requisito de aceite: derruba o servico do Gemma de verdade (nivel
    httpx) e confirma que o dominio NUNCA e descartado -- vai para
    investigacao normal (fail-open), com gemma_fallback_total contabilizado."""
    published, discards = _wire_mocks
    import asyncio

    with patch("gemma_triage.httpx.AsyncClient", _DownAsyncClient()):
        entry = _parsed_entry(["nub4nk-servico-fora-do-ar.xyz"])
        await ctl._process_certificate_entry(entry)
        await asyncio.sleep(0.3)
        await ctl._flush_triage_batch()

    assert discards == [], "FALHA CRITICA: dominio foi descartado com o Gemma fora do ar"
    assert len(published) == 1
    assert published[0]["domain"] == "nub4nk-servico-fora-do-ar.xyz"
    assert published[0]["priority"] == "INVESTIGATE"


@pytest.mark.asyncio
async def test_batch_flushes_automatically_at_max_size(_wire_mocks):
    """Requisito 4 (batching): o lote deve fechar sozinho ao atingir
    `gemma_batch_max_size`, sem esperar a janela de tempo."""
    published, _ = _wire_mocks
    import asyncio

    from config import settings

    call_count = 0

    async def fake_triage_batch(signals):
        nonlocal call_count
        call_count += 1
        results = {
            s.domain: TriageResult(
                domain=s.domain, verdict="INVESTIGATE", risk_score=0.5,
                target_brand=s.target_brand, rationale="teste",
            )
            for s in signals
        }
        return TriageBatchOutcome(
            results=results, latency_ms=10.0, cost_usd=0.0,
            fallback_used=False, model_id="gemma3:270m",
        )

    with patch("plane1_ingestion.ct_listener.triage_batch", fake_triage_batch):
        domains = [f"nub4nk-lote-{i}.xyz" for i in range(settings.gemma_batch_max_size)]
        entry = _parsed_entry(domains)
        await ctl._process_certificate_entry(entry)
        await asyncio.sleep(0.3)

    assert call_count == 1, "o lote deveria ter fechado sozinho ao bater o tamanho maximo"
    assert len(published) == settings.gemma_batch_max_size
