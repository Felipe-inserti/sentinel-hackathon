"""Camada de triagem intermediaria (Gemma 3 270M via Ollama).

Fica entre `prefilter.py` (matematica pura, custo zero) e o orquestrador
(`orchestrator.py`, scraping + Gemini, caro): uma cascata de tres niveis
em que cada estagio e mais caro e mais raro que o anterior.

## Escolha de modelo e serving (decidido e confirmado com o usuario)

- **Modelo: Gemma 3 270M** (270M parametros -- 170M de embedding, 100M de
  transformer; texto puro, nao multimodal). Verificado no anuncio oficial
  (developers.googleblog.com/en/introducing-gemma-3-270m/): e a MENOR
  variante Gemma atual e foi desenhada especificamente para "sentiment
  analysis, entity extraction, query routing" -- e exatamente o que esta
  camada faz (roteamento/triagem, nao geracao livre). O Gemma 4 (mais
  recente, abr/2026) so tem variantes multimodais a partir de 2.3B
  parametros efetivos (E2B) -- maior e sem necessidade para esta tarefa.

- **Serving: Cloud Run CPU (sem GPU) rodando Ollama**, nao Vertex AI Model
  Garden nem a API gerenciada do Gemini. Motivos (verificados, nao
  chutados):
    1. A API gerenciada da Gemini API so expoe Gemma 4 GRANDE (31B e
       26B MoE) -- confirmado em ai.google.dev/gemma/docs/core/gemma_on_gemini_api.
       O 270M nao esta disponivel por essa via.
    2. Endpoints do Vertex AI Model Garden cobram por node-hora CONTINUA
       enquanto implantados, independente de trafego (confirmado: so o
       node CPU ja sai a ~$0.077/h; GPU custa muito mais) -- nao escala a
       zero sozinho, exige disciplina manual de undeploy. Contradiz
       "custo perto de zero fora da janela de demo".
    3. Cloud Run escala a zero AUTOMATICAMENTE por padrao (sem GPU e sem
       nenhuma flag especial -- e o comportamento default do produto,
       GPU e que e opt-in). Um modelo de 292MB roda bem em CPU para uma
       tarefa de classificacao curta. Ver scripts/deploy_gemma_cloudrun.sh
       para deploy/teardown.
    4. Ollama suporta saida JSON restrita por schema Pydantic via o campo
       `format` (decodificacao restrita, nao so "tente gerar JSON") desde
       a v0.3.0 -- confirmado em docs.ollama.com/capabilities/structured-outputs.
       Usado aqui para validar `TriageBatchResponse` de forma confiavel.

## Fail-open (requisito de seguranca do produto)

Se o Ollama estiver fora do ar ou devolver algo que nao valida contra o
schema apos 1 retentativa, TODOS os dominios do lote viram INVESTIGATE
(nunca DISCARD por omissao) -- perder um phishing de verdade por falha de
infraestrutura seria um falso negativo silencioso, o unico erro caro desta
camada. `gemma_fallback_total` registra quando isso acontece.

Este modulo nao depende de `telemetry`/Firestore -- quem chama
(`ct_listener.py`) e responsavel por abrir spans/contadores por dominio,
seguindo o mesmo padrao de `sanitizer.py`/`llm_client.py` no projeto.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from config import settings
from sanitizer import sanitize

# TODO(agent-registry): publicar "gemma-triage-agent" no Agent Registry do
# projeto quando esse mecanismo existir. Nao integrado neste sprint -- nao
# ha cliente/API de Agent Registry no projeto ainda, e inventar o formato
# de publicacao seria o tipo de chute que este projeto evita (ver
# CLAUDE.md e FINDINGS.md secao 8). Confirmar com o time qual Agent
# Registry (interno? Vertex AI Agent Builder? outro?) antes de implementar.

logger = logging.getLogger("gemma_triage")


class DomainSignals(BaseModel):
    """Sinais baratos e ja disponiveis, sem nenhum I/O de rede novo --
    nada de scraping/HTML aqui, e isso que torna esta camada barata."""

    domain: str
    target_brand: str | None = None
    similarity_score: float
    heuristics_triggered: list[str] = Field(default_factory=list)
    domain_tokens: list[str] = Field(default_factory=list)
    tld: str
    certificate_age_seconds: float | None = None


class TriageResult(BaseModel):
    domain: str
    verdict: Literal["DISCARD", "INVESTIGATE", "ESCALATE_IMMEDIATE"]
    risk_score: float = Field(ge=0.0, le=1.0)
    target_brand: str | None = None
    rationale: str


class TriageBatchResponse(BaseModel):
    """Schema passado ao Ollama via `format=` -- decodificacao restrita
    garante que a saida sempre bate com isso, um item por dominio."""

    results: list[TriageResult]


class TriageBatchOutcome(BaseModel):
    results: dict[str, TriageResult]
    latency_ms: float
    cost_usd: float
    fallback_used: bool
    model_id: str

    def per_domain_share(self) -> tuple[float, float]:
        """Fatia justa de latencia/custo do lote por dominio, para
        atribuir ao span `gemma.triage` de cada um (a chamada e uma so,
        compartilhada entre todos os itens do lote)."""
        n = max(len(self.results), 1)
        return self.latency_ms / n, self.cost_usd / n


TRIAGE_SYSTEM_PROMPT = """Voce e um classificador de triagem rapida para deteccao de \
phishing B2B. Recebe uma lista de dominios com sinais ESTRUTURADOS \
(nunca o conteudo da pagina -- scraping so acontece depois, se voce \
mandar para investigacao). Devolva um veredito por dominio.

Classifique cada dominio em:
- DISCARD: sinais fracos, provavelmente falso positivo do prefiltro \
matematico que ja rodou antes de voce (ex: similaridade moderada mas sem \
heuristica forte de ofuscacao, certificado antigo, TLD formal).
- INVESTIGATE: sinais moderados a fortes de typosquatting -- merece \
raspagem completa e analise pelo Gemini.
- ESCALATE_IMMEDIATE: sinais gritantes -- similaridade muito alta com \
marca financeira/bancaria E certificado emitido ha poucos minutos \
(dominio recem-registrado, ataque iminente).

Priorize RECALL sobre precisao: e muito pior descartar um phishing real \
(falso negativo, o unico erro caro desta camada) do que mandar um \
dominio inocente para investigacao. Na duvida, NUNCA escolha DISCARD.

Exemplos (padroes reais de typosquatting de marcas brasileiras):

Entrada: {"domain": "nub4nk-suporte-oficial.xyz", "target_brand": "nubank", \
"similarity_score": 0.95, "heuristics_triggered": ["leetspeak", "sliding_window"], \
"domain_tokens": ["suporte", "oficial"], "tld": "xyz", "certificate_age_seconds": 180}
Saida: {"domain": "nub4nk-suporte-oficial.xyz", "verdict": "ESCALATE_IMMEDIATE", \
"risk_score": 0.97, "target_brand": "nubank", "rationale": "Leetspeak de marca \
bancaria + certificado com minutos de vida, padrao classico de ataque iminente"}

Entrada: {"domain": "loggi-entregas-rastreio.com", "target_brand": "loggi", \
"similarity_score": 0.88, "heuristics_triggered": ["sliding_window"], \
"domain_tokens": ["entregas", "rastreio"], "tld": "com", "certificate_age_seconds": 5400000}
Saida: {"domain": "loggi-entregas-rastreio.com", "verdict": "INVESTIGATE", \
"risk_score": 0.72, "target_brand": "loggi", "rationale": "Marca de logistica \
com termos de rastreio tipicos de phishing, sem sinal de urgencia extrema"}

Entrada: {"domain": "promocoes-ifood-parceiros.com.br", "target_brand": "ifood", \
"similarity_score": 0.58, "heuristics_triggered": ["sliding_window"], \
"domain_tokens": ["promocoes", "parceiros"], "tld": "com.br", "certificate_age_seconds": 63072000}
Saida: {"domain": "promocoes-ifood-parceiros.com.br", "verdict": "DISCARD", \
"risk_score": 0.15, "target_brand": "ifood", "rationale": "Similaridade \
moderada mas certificado com 2 anos, TLD .com.br formal, sem homoglyph \
nem leetspeak -- perfil de negocio parceiro legitimo, nao ataque novo"}

Agora classifique os dominios a seguir. Responda APENAS com o JSON do \
schema pedido, um resultado por dominio, na mesma ordem."""


def _sanitize_signals(signals: DomainSignals) -> DomainSignals:
    """O nome do dominio (e seus tokens) e conteudo raspado/hostil por
    definicao mesmo sem HTML -- pode carregar homoglyph e caractere
    invisivel (ver CLAUDE.md e sanitizer.py). Sanitiza antes de montar o
    prompt, mesmo essa entrada sendo pequena."""
    clean_domain = sanitize(signals.domain).clean_text
    clean_tokens = [sanitize(t).clean_text for t in signals.domain_tokens]
    return signals.model_copy(update={"domain": clean_domain, "domain_tokens": clean_tokens})


def _estimate_cost_usd(latency_ms: float) -> float:
    """Custo aproximado de CPU/memoria do Cloud Run para a duracao da
    chamada (nao e cobranca por token -- e self-hosted). Ver os
    comentarios de `config.py` sobre a fonte do preco."""
    seconds = latency_ms / 1000
    vcpu_cost = seconds * settings.gemma_cloud_run_vcpu_count * settings.cloud_run_cpu_price_per_vcpu_second_usd
    mem_cost = seconds * settings.gemma_cloud_run_memory_gib * settings.cloud_run_memory_price_per_gib_second_usd
    return vcpu_cost + mem_cost


async def _call_ollama_once(batch: list[DomainSignals]) -> tuple[TriageBatchResponse, float]:
    """Uma unica tentativa. Levanta excecao em qualquer falha (rede,
    timeout, HTTP nao-2xx, JSON invalido, schema invalido) -- quem decide
    sobre retry/fail-open e `triage_batch`."""
    payload = {
        "model": settings.gemma_model_id,
        "messages": [
            {"role": "system", "content": TRIAGE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps([s.model_dump() for s in batch], ensure_ascii=False),
            },
        ],
        "stream": False,
        "format": TriageBatchResponse.model_json_schema(),
        "options": {"temperature": 0.1},
    }

    start = time.monotonic()
    async with httpx.AsyncClient(timeout=settings.gemma_request_timeout_seconds) as client:
        response = await client.post(
            f"{settings.gemma_ollama_base_url}/api/chat", json=payload
        )
        response.raise_for_status()
    latency_ms = (time.monotonic() - start) * 1000

    body = response.json()
    content = body["message"]["content"]
    parsed = TriageBatchResponse.model_validate_json(content)
    return parsed, latency_ms


async def triage_batch(signals: list[DomainSignals]) -> TriageBatchOutcome:
    """Classifica um lote de dominios em uma unica chamada ao Gemma
    (amortiza o custo fixo de inferencia sobre o lote inteiro). Fail-open
    em qualquer falha persistente: todo o lote vira INVESTIGATE."""
    if not signals:
        return TriageBatchOutcome(
            results={}, latency_ms=0.0, cost_usd=0.0, fallback_used=False,
            model_id=settings.gemma_model_id,
        )

    sanitized_batch = [_sanitize_signals(s) for s in signals]

    last_error: Exception | None = None
    for attempt in range(settings.gemma_max_retries + 1):
        try:
            parsed, latency_ms = await _call_ollama_once(sanitized_batch)
            results = {r.domain: r for r in parsed.results}
            missing = [s.domain for s in sanitized_batch if s.domain not in results]
            if missing:
                raise ValueError(f"Gemma nao devolveu veredito para: {missing}")

            return TriageBatchOutcome(
                results=results,
                latency_ms=latency_ms,
                cost_usd=_estimate_cost_usd(latency_ms),
                fallback_used=False,
                model_id=settings.gemma_model_id,
            )
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Tentativa %d/%d de triagem Gemma falhou: %s",
                attempt + 1,
                settings.gemma_max_retries + 1,
                exc,
            )

    logger.error(
        "Gemma indisponivel/invalido apos %d tentativas -- fail-open para "
        "INVESTIGATE em %d dominios (%s)",
        settings.gemma_max_retries + 1,
        len(sanitized_batch),
        last_error,
    )
    fallback_results = {
        s.domain: TriageResult(
            domain=s.domain,
            verdict="INVESTIGATE",
            risk_score=0.5,
            target_brand=s.target_brand,
            rationale=(
                f"Fail-open: triagem Gemma indisponivel ou invalida "
                f"({last_error.__class__.__name__ if last_error else 'erro desconhecido'})"
            ),
        )
        for s in sanitized_batch
    }
    return TriageBatchOutcome(
        results=fallback_results,
        latency_ms=0.0,
        cost_usd=0.0,
        fallback_used=True,
        model_id=settings.gemma_model_id,
    )
