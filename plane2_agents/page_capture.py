"""Captura de screenshot para CLASSIFICACAO (Sprint multimodal) -- Playwright.

Modulo NOVO e deliberadamente pequeno, isolado de `evidence_agent.py` por
decisao explicita do sprint: `evidence_agent.py` roda em outro Cloud Run
Job, com outra imagem Docker (`Dockerfile.evidence`), e so captura o
screenshot DEPOIS do veredito (so para dominios ja MALICIOUS, ver
docstring daquele modulo). Importar sua funcao privada de captura
acoplaria dois processos/duas imagens Docker por uma razao errada.

Dois artefatos com propositos DISTINTOS, coexistindo de proposito:
  - Este modulo (`capture_page_screenshot`): screenshot para DECIDIR --
    usado por `plane2_agents.orchestrator.classify_domain_with_gemini` no
    MESMO ponto onde ja roda `scrape_website` (I/O de rede contra o
    alvo). Fica em memoria, vira `inline_data` de UMA chamada Gemini, e e
    descartado -- NUNCA sobe ao GCS, nunca e persistido em lugar nenhum
    por este modulo.
  - `evidence_agent.py::_capture_screenshot_and_form_signal` (inalterado):
    screenshot para PROVAR -- persistido no GCS com hash sha256, vira
    parte do dossie de evidencia mostrado ao revisor humano.

Reduz a imagem ANTES de qualquer byte sair para o modelo: viewport FIXO
(~768px de largura -- a MESMA renderizacao do Playwright ja acontece
nessa largura, sem precisar de Pillow/lib de imagem so para reamostrar
depois) + screenshot em JPEG com qualidade moderada (`Page.screenshot`
aceita `type`/`quality` nativamente, documentado na API oficial do
Playwright) -- nenhuma dependencia nova.

Sandbox de navegacao: mesmo mecanismo de `evidence_agent._domain_lock_router`
(dominio travado, so bloqueia NAVEGACAO para fora do dominio alvo, nunca
sub-recursos) -- duplicado aqui de proposito, nao importado (ver acima).
Nenhuma chamada usa `page.evaluate()` com string vinda do conteudo
raspado -- este modulo nao interage com formulario nenhum, so tira uma
foto.

Fail-safe: `capture_page_screenshot` NUNCA levanta. Qualquer falha
(timeout, DNS, TLS, browser ausente/quebrado) devolve `None` -- o
chamador (`classify_domain_with_gemini`) segue classificando so com texto
e marca `AnalysisResult.visual_analysis_available=False` (ver
orchestrator.py). O pipeline de classificacao nunca quebra por ausencia
de imagem.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

logger = logging.getLogger("page_capture")

# Mesmo timeout agressivo de evidence_agent.py (PLAYWRIGHT_TIMEOUT_MS) --
# esta e uma chamada de CLASSIFICACAO em caminho quente, nao pode travar o
# pipeline esperando um site lento/fora do ar.
PLAYWRIGHT_TIMEOUT_MS = 15_000

# "largura maxima ~768px" (pedido explicito do sprint) -- viewport fixo,
# nao um teto de reamostragem pos-captura.
CAPTURE_VIEWPORT_WIDTH = 768
CAPTURE_VIEWPORT_HEIGHT = 1024  # ponto de partida -- full_page=True captura a altura real da pagina

# "JPEG com qualidade moderada" -- 70 e o padrao comum de "moderado" do
# proprio Playwright/libs de imagem (bom equilibrio tamanho/legibilidade
# para um logo/formulario, nao para fotografia de alta fidelidade).
JPEG_QUALITY = 70
SCREENSHOT_MIME_TYPE = "image/jpeg"


async def _domain_lock_router(route, request, target_domain: str) -> None:
    """So intercepta NAVEGACAO (documento/iframe) -- sub-recursos
    (imagem/CSS/fonte) de outros hosts continuam liberados, senao o
    screenshot sai quebrado visualmente. Mesma regra de
    `evidence_agent._domain_lock_router`, duplicada de proposito (ver
    docstring do modulo)."""
    if request.is_navigation_request():
        hostname = urlparse(request.url).hostname or ""
        if hostname != target_domain and not hostname.endswith(f".{target_domain}"):
            logger.warning(
                "Navegacao bloqueada por sair do dominio alvo (%s): %s", target_domain, request.url
            )
            await route.abort()
            return
    await route.continue_()


async def capture_page_screenshot(url: str, domain: str) -> bytes | None:
    """Captura um screenshot JPEG reduzido de `url` (navegacao travada a
    `domain`), para uso IMEDIATO como `inline_data` de uma unica chamada
    Gemini -- ver `plane2_agents.orchestrator.classify_domain_with_gemini`.

    Nunca levanta: qualquer falha devolve `None` (ver docstring do
    modulo). Bytes retornados nunca sao persistidos por este modulo -- a
    decisao de subir ao GCS pertence exclusivamente a `evidence_agent.py`,
    que roda depois e independentemente."""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, timeout=PLAYWRIGHT_TIMEOUT_MS)
            try:
                context = await browser.new_context(
                    viewport={"width": CAPTURE_VIEWPORT_WIDTH, "height": CAPTURE_VIEWPORT_HEIGHT},
                    ignore_https_errors=True,
                )
                page = await context.new_page()
                await page.route("**/*", lambda route, request: _domain_lock_router(route, request, domain))
                await page.goto(url, timeout=PLAYWRIGHT_TIMEOUT_MS, wait_until="domcontentloaded")
                return await page.screenshot(
                    full_page=True,
                    type="jpeg",
                    quality=JPEG_QUALITY,
                    timeout=PLAYWRIGHT_TIMEOUT_MS,
                )
            finally:
                await browser.close()
    except (PlaywrightError, PlaywrightTimeoutError) as exc:
        logger.warning("Falha ao capturar screenshot de classificacao para %s: %s", domain, exc)
        return None
    except Exception:  # defesa extra -- captura de tela nunca derruba a classificacao
        logger.exception("Erro inesperado capturando screenshot de classificacao para %s", domain)
        return None
