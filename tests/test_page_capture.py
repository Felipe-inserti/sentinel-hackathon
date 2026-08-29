"""Testes de `plane2_agents/page_capture.py` (Sprint multimodal) --
Playwright sempre mockado (nenhum Chromium real e aberto, ver
docstring do modulo)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

import plane2_agents.page_capture as pc


def _fake_playwright(*, screenshot_bytes: bytes = b"fake-jpeg-bytes", goto_side_effect=None, screenshot_side_effect=None):
    """Monta a cadeia async_playwright() -> p.chromium.launch() -> browser
    .new_context() -> context.new_page() -> page, com os metodos usados
    por `capture_page_screenshot` mockados. Devolve (context_manager, page)
    para o teste inspecionar chamadas em `page`."""
    page = MagicMock()
    page.route = AsyncMock()
    page.goto = AsyncMock(side_effect=goto_side_effect)
    page.screenshot = AsyncMock(side_effect=screenshot_side_effect, return_value=screenshot_bytes)

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

    return _cm(), page, browser, context


@pytest.mark.asyncio
async def test_capture_page_screenshot_returns_bytes_on_success(monkeypatch):
    cm, page, browser, context = _fake_playwright(screenshot_bytes=b"\xff\xd8\xff\xe0jpeg")
    monkeypatch.setattr(pc, "async_playwright", lambda: cm)

    result = await pc.capture_page_screenshot("https://banco-teste-fake.com", "banco-teste-fake.com")

    assert result == b"\xff\xd8\xff\xe0jpeg"
    browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_capture_page_screenshot_uses_fixed_viewport_and_jpeg_quality(monkeypatch):
    cm, page, browser, context = _fake_playwright()
    monkeypatch.setattr(pc, "async_playwright", lambda: cm)

    await pc.capture_page_screenshot("https://banco-teste-fake.com", "banco-teste-fake.com")

    _, kwargs = browser.new_context.call_args
    assert kwargs["viewport"] == {"width": pc.CAPTURE_VIEWPORT_WIDTH, "height": pc.CAPTURE_VIEWPORT_HEIGHT}

    _, screenshot_kwargs = page.screenshot.call_args
    assert screenshot_kwargs["type"] == "jpeg"
    assert screenshot_kwargs["quality"] == pc.JPEG_QUALITY
    assert screenshot_kwargs["full_page"] is True


@pytest.mark.asyncio
async def test_capture_page_screenshot_returns_none_on_timeout(monkeypatch):
    cm, page, browser, context = _fake_playwright(
        goto_side_effect=PlaywrightTimeoutError("timeout ao navegar")
    )
    monkeypatch.setattr(pc, "async_playwright", lambda: cm)

    result = await pc.capture_page_screenshot("https://lento.test", "lento.test")

    assert result is None
    browser.close.assert_awaited_once()  # sempre fecha o browser, mesmo em falha


@pytest.mark.asyncio
async def test_capture_page_screenshot_returns_none_on_playwright_error(monkeypatch):
    cm, page, browser, context = _fake_playwright(
        screenshot_side_effect=PlaywrightError("pagina fechada inesperadamente")
    )
    monkeypatch.setattr(pc, "async_playwright", lambda: cm)

    result = await pc.capture_page_screenshot("https://quebrado.test", "quebrado.test")

    assert result is None


@pytest.mark.asyncio
async def test_capture_page_screenshot_never_raises_on_unexpected_exception(monkeypatch):
    """Defesa extra alem de PlaywrightError/TimeoutError -- nenhuma
    excecao pode propagar e derrubar a classificacao."""
    cm, page, browser, context = _fake_playwright(goto_side_effect=RuntimeError("bug inesperado"))
    monkeypatch.setattr(pc, "async_playwright", lambda: cm)

    result = await pc.capture_page_screenshot("https://bug.test", "bug.test")

    assert result is None


@pytest.mark.asyncio
async def test_capture_page_screenshot_installs_domain_lock_route(monkeypatch):
    cm, page, browser, context = _fake_playwright()
    monkeypatch.setattr(pc, "async_playwright", lambda: cm)

    await pc.capture_page_screenshot("https://banco-teste-fake.com", "banco-teste-fake.com")

    page.route.assert_awaited_once()
    args, _ = page.route.call_args
    assert args[0] == "**/*"


# --- _domain_lock_router: mesma regra de evidence_agent (dominio travado) --


@pytest.mark.asyncio
async def test_domain_lock_router_aborts_navigation_to_other_host():
    route = MagicMock()
    route.abort = AsyncMock()
    route.continue_ = AsyncMock()
    request = MagicMock()
    request.is_navigation_request.return_value = True
    request.url = "https://atacante-externo.example/pagina"

    await pc._domain_lock_router(route, request, "banco-teste-fake.com")

    route.abort.assert_awaited_once()
    route.continue_.assert_not_called()


@pytest.mark.asyncio
async def test_domain_lock_router_allows_navigation_to_same_domain():
    route = MagicMock()
    route.abort = AsyncMock()
    route.continue_ = AsyncMock()
    request = MagicMock()
    request.is_navigation_request.return_value = True
    request.url = "https://banco-teste-fake.com/login"

    await pc._domain_lock_router(route, request, "banco-teste-fake.com")

    route.continue_.assert_awaited_once()
    route.abort.assert_not_called()


@pytest.mark.asyncio
async def test_domain_lock_router_allows_subresources_from_other_hosts():
    """So bloqueia NAVEGACAO -- sub-recursos (imagem/CSS/fonte) de outros
    hosts continuam liberados, senao o screenshot sai quebrado
    visualmente (mesma regra de evidence_agent._domain_lock_router)."""
    route = MagicMock()
    route.abort = AsyncMock()
    route.continue_ = AsyncMock()
    request = MagicMock()
    request.is_navigation_request.return_value = False
    request.url = "https://cdn-externo.example/logo.png"

    await pc._domain_lock_router(route, request, "banco-teste-fake.com")

    route.continue_.assert_awaited_once()
    route.abort.assert_not_called()
