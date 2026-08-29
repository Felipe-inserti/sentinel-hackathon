"""Teste adversarial de regressao -- Sprint 2, Stage D.

Achado real (nao so um ajuste de demo): `classify_domain_with_gemini`
passava o campo `domain` CRU para `page_capture.capture_page_screenshot`,
que o usa como `target_domain` da trava de navegacao
(`page_capture._domain_lock_router`). Em producao real `domain` e sempre
um hostname puro (CN/SAN de certificado, via `ct_listener.py`), entao isso
nunca quebrou -- mas nada no schema (`SuspiciousDomainSignal.domain: str`,
ver seed_registry.py) garante isso. Um alvo de teste com URL por CAMINHO
(`https://bucket.storage.googleapis.com/malicious.html`, necessario porque
GCS so serve `mainPageSuffix` atras de um Load Balancer com dominio
proprio -- ver infra/demo_target_bucket.tf) expos isso: a trava comparava
o hostname real da navegacao contra a string `domain` inteira (COM o
path), nunca batia, e abortava ate a navegacao inicial LEGITIMA -- falso
positivo de seguranca, nao so um detalhe do teste.

Corrigido em `classify_domain_with_gemini` (plane2_agents/orchestrator.py):
o parametro de trava agora vem do HOSTNAME de `target_url` (a URL de fato
navegada, via `urlparse().hostname`), nunca do campo `domain` cru.
`page_capture.py` (incluindo `_domain_lock_router`) NAO MUDOU -- os 3
testes existentes em `tests/test_page_capture.py` continuam passando sem
nenhuma alteracao (mesma trava, mesma funcao, so quem a chama passa um
argumento derivado corretamente agora).

Este arquivo testa a INTEGRACAO real (orchestrator -> page_capture ->
_domain_lock_router de verdade, so o Chromium mockado) -- os 3 casos
minimos pedidos:
  1. target_url COM path, redirect pra outro host -> BLOQUEIA
  2. target_url COM path, navegacao no mesmo host -> PERMITE
  3. target_url SEM path (caso de producao hoje) -> comportamento inalterado
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

import plane2_agents.orchestrator as orch
from llm_client import LLMResult, LLMUsage


def _llm_result(data) -> LLMResult:
    return LLMResult(data=data, usage=LLMUsage(model_id="teste", input_tokens=10, output_tokens=5, latency_ms=1.0))


def _fake_playwright_capturing_route_handler(captured: dict):
    """Monta a cadeia async_playwright() -> chromium.launch() ->
    new_context() -> new_page() como em tests/test_page_capture.py, mas
    GRAVA o handler passado a `page.route(...)` em `captured["handler"]`
    para o teste poder invoca-lo diretamente com requests sinteticas --
    e assim que exercita `_domain_lock_router` REAL com o `target_domain`
    que `classify_domain_with_gemini` de fato derivou e passou."""
    page = MagicMock()

    async def _route(pattern, handler):
        captured["handler"] = handler

    page.route = AsyncMock(side_effect=_route)
    page.goto = AsyncMock()
    page.screenshot = AsyncMock(return_value=b"fake-jpeg-bytes")

    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)

    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock()

    p = MagicMock()
    p.chromium.launch = AsyncMock(return_value=browser)

    @asynccontextmanager
    async def _cm():
        yield p

    return _cm()


def _fake_route(url: str, is_navigation: bool = True) -> MagicMock:
    route = MagicMock()
    route.abort = AsyncMock()
    route.continue_ = AsyncMock()
    request = MagicMock()
    request.is_navigation_request.return_value = is_navigation
    request.url = url
    return route, request


@pytest.mark.asyncio
async def test_path_style_target_url_still_blocks_navigation_to_other_host(monkeypatch):
    """Caso 1: target_url COM path (alvo do bucket GCS) -- redirect pra
    outro host continua BLOQUEADO. Prova que a correcao nao afrouxou nada:
    a trava usa o hostname certo, nao um path-string que nunca bateria com
    nada (o que seria uma falha ABERTA, nao fechada, se a derivacao
    estivesse errada na direcao oposta)."""
    monkeypatch.setattr(orch, "scrape_website", lambda url: "conteudo da pagina de teste")
    monkeypatch.setattr(
        orch.llm_client, "generate",
        AsyncMock(return_value=_llm_result(orch.AnalysisResult(classification="SAFE", confidence=0.8, reasoning="ok"))),
    )

    captured: dict = {}
    monkeypatch.setattr(orch.page_capture, "async_playwright", lambda: _fake_playwright_capturing_route_handler(captured))

    await orch.classify_domain_with_gemini(
        "seu-id-unico-sentinel-demo-target.storage.googleapis.com/malicious.html", "bancoteste", None
    )

    assert "handler" in captured, "page.route nunca foi chamado -- capture_page_screenshot nao rodou"
    route, request = _fake_route("https://atacante-externo.example/pagina", is_navigation=True)
    await captured["handler"](route, request)

    route.abort.assert_awaited_once()
    route.continue_.assert_not_called()


@pytest.mark.asyncio
async def test_path_style_target_url_allows_navigation_to_same_host(monkeypatch):
    """Caso 2: target_url COM path -- navegacao para o MESMO host (a
    pagina real que estamos capturando) e PERMITIDA. Sem isso a propria
    navegacao inicial para o alvo de teste (bucket GCS por path) seria
    abortada -- o bug real que o Stage D encontrou."""
    monkeypatch.setattr(orch, "scrape_website", lambda url: "conteudo da pagina de teste")
    monkeypatch.setattr(
        orch.llm_client, "generate",
        AsyncMock(return_value=_llm_result(orch.AnalysisResult(classification="SAFE", confidence=0.8, reasoning="ok"))),
    )

    captured: dict = {}
    monkeypatch.setattr(orch.page_capture, "async_playwright", lambda: _fake_playwright_capturing_route_handler(captured))

    await orch.classify_domain_with_gemini(
        "seu-id-unico-sentinel-demo-target.storage.googleapis.com/malicious.html", "bancoteste", None
    )

    assert "handler" in captured
    route, request = _fake_route(
        "https://seu-id-unico-sentinel-demo-target.storage.googleapis.com/malicious.html", is_navigation=True
    )
    await captured["handler"](route, request)

    route.continue_.assert_awaited_once()
    route.abort.assert_not_called()


@pytest.mark.asyncio
async def test_bare_domain_target_url_behavior_unchanged(monkeypatch):
    """Caso 3: target_url SEM path (caso de producao hoje -- domain vindo
    de ct_listener.py e sempre um hostname puro) -- comportamento
    inalterado: hostname derivado de target_url == o proprio `domain`
    original, exatamente como antes da correcao."""
    monkeypatch.setattr(orch, "scrape_website", lambda url: "conteudo da pagina de teste")
    monkeypatch.setattr(
        orch.llm_client, "generate",
        AsyncMock(return_value=_llm_result(orch.AnalysisResult(classification="SAFE", confidence=0.8, reasoning="ok"))),
    )

    captured: dict = {}
    monkeypatch.setattr(orch.page_capture, "async_playwright", lambda: _fake_playwright_capturing_route_handler(captured))

    await orch.classify_domain_with_gemini("banco-teste-fake.com", "bancoteste", None)

    assert "handler" in captured
    # mesmo host do dominio original -> permite (comportamento de sempre)
    route_ok, request_ok = _fake_route("https://banco-teste-fake.com/pagina", is_navigation=True)
    await captured["handler"](route_ok, request_ok)
    route_ok.continue_.assert_awaited_once()
    route_ok.abort.assert_not_called()

    # host diferente -> continua bloqueando (comportamento de sempre)
    route_blocked, request_blocked = _fake_route("https://atacante-externo.example/pagina", is_navigation=True)
    await captured["handler"](route_blocked, request_blocked)
    route_blocked.abort.assert_awaited_once()
    route_blocked.continue_.assert_not_called()
