"""Plano 1 - Ingestao (Certificate Transparency Listener).

Conecta-se ao stream publico de Certificate Transparency (certstream), roda
cada dominio recem-emitido pelo `prefilter` (matematica pura, zero LLM) e,
para os sobreviventes, agrupa em lotes para a triagem intermediaria do
Gemma (`gemma_triage.py`) antes de publicar no Pub/Sub para o Plano 2
(scraping + Gemini, caro). Cascata de tres niveis, cada estagio mais caro
e mais raro que o anterior:

    prefiltro (matematica, custo zero)
      -> Gemma 3 270M em lote (CPU, self-hosted, quase zero)
        -> Gemini + scraping (Plano 2, caro)

Cada dominio processado abre seu proprio trace (`ct.ingest`), com
`prefilter.evaluate`, `gemma.triage` e, se for para investigacao,
`pubsub.publish` como filhos. O `traceparent` W3C e injetado nos atributos
da mensagem do Pub/Sub para o Plano 2 continuar o MESMO trace do outro
lado (ver `telemetry.py`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from certstream.core import CertStreamClient
from google.api_core.exceptions import GoogleAPICallError
from google.cloud import firestore, pubsub_v1
from opentelemetry import context as otel_context
from opentelemetry.context import Context

import observation_run
import telemetry
from config import settings
from gemma_triage import DomainSignals, TriageResult, triage_batch
from plane1_ingestion.prefilter import DomainRiskAssessment, analyze_domain, normalize_domain

# `telemetry.setup()` configura o logging JSON estruturado no logger raiz
# (`configure_json_logging`) -- todo `logging.getLogger(...)` daqui em
# diante ja sai formatado e correlacionado com o span ativo.
tracer = telemetry.setup("sentinel-ct-listener")
logger = logging.getLogger("ct_listener")

CERTSTREAM_URL = os.environ.get("CERTSTREAM_URL", "wss://certstream.calidog.io")

# Backoff de reconexao ao websocket do certstream.
_RECONNECT_MIN_DELAY_SECONDS = 2
_RECONNECT_MAX_DELAY_SECONDS = 60

# Espelhamento em lote das metricas no Firestore: escrever a cada dominio
# seria muito volume (certstream processa varios eventos/segundo, ~99%
# descartados). Acumula localmente e grava um unico documento a cada N.
_METRICS_FLUSH_INTERVAL = 500

_publisher = pubsub_v1.PublisherClient()
_topic_path = _publisher.topic_path(settings.gcp_project_id, settings.suspicious_topic_id)
_firestore_db = firestore.Client()

_processed_count = 0
_suspicious_count = 0
_batch_deltas: dict[str, int] = {}

# Fila de sobreviventes do prefiltro aguardando triagem em lote pelo Gemma.
# So existe apos `main()` iniciar (precisa do event loop rodando) --
# `handle_certstream_message` roda numa thread separada (callback sincrono
# do certstream) e usa `asyncio.run_coroutine_threadsafe` para agendar no
# loop, mesmo padrao ja usado em orchestrator.py para o Pub/Sub.
_loop: asyncio.AbstractEventLoop | None = None
_pending_batch: list["_PendingTriageItem"] = []


@dataclass
class _PendingTriageItem:
    domain: str
    assessment: DomainRiskAssessment
    certificate_age_seconds: float | None
    otel_context: Context


def _on_publish_done(future: Future) -> None:
    try:
        future.result()
    except GoogleAPICallError:
        logger.exception("Falha ao publicar dominio suspeito no Pub/Sub")


def _publish_suspicious_domain(domain: str, brand: str | None, score: float, priority: str) -> None:
    payload = {
        "domain": domain,
        "matched_brand": brand,
        "prefilter_score": score,
        "priority": priority,
        "source": "certstream",
        "detected_at": time.time(),
    }
    data = json.dumps(payload).encode("utf-8")

    # Propaga o trace atraves do Pub/Sub: injeta o traceparent W3C nos
    # atributos da mensagem, capturando o span ATIVO no momento da chamada
    # (o `with tracer.start_as_current_span("pubsub.publish")` do
    # chamador) -- sem isso o trace termina aqui e o requisito
    # "ponta a ponta" nao e cumprido.
    carrier: dict[str, str] = {}
    telemetry.inject_traceparent(carrier)

    future = _publisher.publish(
        _topic_path,
        data=data,
        domain=domain,
        matched_brand=brand or "",
        priority=priority,
        **carrier,
    )
    future.add_done_callback(_on_publish_done)


def _extract_domains(message: dict[str, Any]) -> list[str]:
    leaf_cert = message.get("data", {}).get("leaf_cert", {})
    domains = leaf_cert.get("all_domains", [])
    return [d for d in domains if d and not d.startswith("*.*")]


def _extract_certificate_age_seconds(message: dict[str, Any]) -> float | None:
    """`leaf_cert.not_before` (timestamp Unix) e um campo padrao do
    formato de mensagem do certstream. Se ausente/no formato inesperado,
    devolve None em vez de quebrar -- e so um sinal auxiliar para o
    Gemma, nao algo que a decisao do prefiltro dependa."""
    try:
        not_before = message.get("data", {}).get("leaf_cert", {}).get("not_before")
        if not_before is None:
            return None
        return max(time.time() - float(not_before), 0.0)
    except (TypeError, ValueError):
        return None


def _extract_tld(domain: str) -> str:
    cleaned = domain.strip().lower().split("/")[0].split(":")[0]
    parts = [p for p in cleaned.split(".") if p]
    if len(parts) >= 3 and parts[-2] in {"com", "net", "org", "gov", "edu"}:
        return ".".join(parts[-2:])
    return parts[-1] if parts else ""


def _bump_batch(field: str, amount: int | float = 1) -> None:
    _batch_deltas[field] = _batch_deltas.get(field, 0) + amount
    # So os contadores inteiros (nao os de USD, que sao fracoes minusculas
    # e nunca deveriam por si so disparar um flush antecipado) contam para
    # o limiar de tamanho do lote.
    total = sum(v for k, v in _batch_deltas.items() if not k.endswith("_usd_total"))
    if total >= _METRICS_FLUSH_INTERVAL:
        _flush_batch()


def _flush_batch() -> None:
    if not _batch_deltas:
        return
    deltas = dict(_batch_deltas)
    try:
        telemetry.flush_metrics_to_firestore(deltas)
    except Exception:
        logger.exception("Falha ao espelhar metricas no Firestore, descartando lote")
    _batch_deltas.clear()

    # Etapa C -- mesmo lote, destino SEPARADO (observation_runs/{run_id},
    # ver observation_run.py): no-op se nenhuma observacao estiver ativa.
    # Reusa os MESMOS nomes de campo acima -- nao uma segunda convencao.
    observation_run.bump(deltas)
    observation_run.check_prefilter_escape_anomaly()


def _record_triage_discard(domain: str, assessment: DomainRiskAssessment, result: TriageResult) -> None:
    """Registra todo DISCARD do Gemma para permitir auditoria de falso
    negativo depois -- requisito explicito: um DISCARD errado e o unico
    erro caro desta camada."""
    doc_ref = _firestore_db.collection(settings.triage_discard_collection).document(domain)
    doc_ref.set(
        {
            "domain": domain,
            "matched_brand": assessment.matched_brand,
            "prefilter_score": assessment.score,
            "heuristics_triggered": list(assessment.heuristics_triggered),
            "gemma_verdict": result.verdict,
            "gemma_risk_score": result.risk_score,
            "gemma_rationale": result.rationale,
            "discarded_at": datetime.now(timezone.utc),
        }
    )


def handle_certstream_message(message: dict[str, Any], context: Any) -> None:
    """Callback sincrono invocado pela lib `certstream` para cada evento."""
    global _processed_count, _suspicious_count

    if message.get("message_type") != "certificate_update":
        return

    try:
        domains = _extract_domains(message)
    except Exception:
        logger.exception("Payload de certstream mal formado, ignorando evento")
        return

    cert_age = _extract_certificate_age_seconds(message)

    for domain in domains:
        _processed_count += 1

        with tracer.start_as_current_span("ct.ingest") as ingest_span:
            ingest_span.set_attribute("domain", domain)
            telemetry.increment_counter("certificates_ingested_total")
            _bump_batch("certificates_ingested_total")

            with tracer.start_as_current_span("prefilter.evaluate") as pf_span:
                try:
                    assessment = analyze_domain(domain)
                except Exception:
                    logger.exception("Erro no prefiltro para dominio %r", domain)
                    pf_span.set_attribute("prefilter.error", True)
                    continue

                pf_span.set_attribute("prefilter.score", assessment.score)
                pf_span.set_attribute("prefilter.reason", assessment.reason)
                pf_span.set_attribute(
                    "prefilter.decision", "suspicious" if assessment.is_suspicious else "discarded"
                )
                if assessment.matched_brand:
                    pf_span.set_attribute("prefilter.matched_brand", assessment.matched_brand)

            if not assessment.is_suspicious:
                telemetry.increment_counter("certificates_discarded_by_prefilter_total")
                _bump_batch("certificates_discarded_by_prefilter_total")
                continue

            _suspicious_count += 1
            logger.info(
                "PREFILTRO SUSPEITO domain=%s brand=%s score=%.2f -- enfileirado p/ triagem Gemma",
                domain,
                assessment.matched_brand,
                assessment.score,
            )

            item = _PendingTriageItem(
                domain=domain,
                assessment=assessment,
                certificate_age_seconds=cert_age,
                otel_context=otel_context.get_current(),
            )
            if _loop is None:
                # Nao deveria acontecer em operacao normal (main() seta
                # _loop antes de conectar ao certstream) -- mas se
                # acontecer, fail-open: nunca perder um dominio suspeito
                # silenciosamente por falta de infraestrutura de fila.
                logger.error(
                    "Fila de triagem nao inicializada -- publicando %s direto, sem passar pelo Gemma",
                    domain,
                )
                with tracer.start_as_current_span("pubsub.publish") as pub_span:
                    pub_span.set_attribute("pubsub.topic", settings.suspicious_topic_id)
                    _publish_suspicious_domain(
                        domain, assessment.matched_brand, assessment.score, priority="normal"
                    )
                continue

            asyncio.run_coroutine_threadsafe(_enqueue_for_triage(item), _loop)

    if _processed_count % 5000 == 0 and _processed_count > 0:
        ratio = _suspicious_count / _processed_count
        logger.info(
            "Estatisticas: %d dominios processados, %d suspeitos (%.3f%%) "
            "-- descarte do prefiltro: %.2f%%",
            _processed_count,
            _suspicious_count,
            ratio * 100,
            (1 - ratio) * 100,
        )


async def _enqueue_for_triage(item: _PendingTriageItem) -> None:
    _pending_batch.append(item)
    if len(_pending_batch) >= settings.gemma_batch_max_size:
        await _flush_triage_batch()


def _build_signals(item: _PendingTriageItem) -> DomainSignals:
    return DomainSignals(
        domain=normalize_domain(item.domain),
        target_brand=item.assessment.matched_brand,
        similarity_score=item.assessment.score,
        heuristics_triggered=list(item.assessment.heuristics_triggered),
        domain_tokens=list(item.assessment.tokens),
        tld=_extract_tld(item.domain),
        certificate_age_seconds=item.certificate_age_seconds,
    )


async def _flush_triage_batch() -> None:
    """Chamada tanto pelo timer periodico (janela curta) quanto quando o
    lote atinge o tamanho maximo -- amortiza o custo fixo de inferencia
    do Gemma sobre varios dominios numa unica chamada."""
    global _pending_batch
    if not _pending_batch:
        return
    batch, _pending_batch = _pending_batch, []

    signals = [_build_signals(item) for item in batch]
    outcome = await triage_batch(signals)
    latency_share, cost_share = outcome.per_domain_share()

    if outcome.fallback_used:
        logger.warning(
            "Lote de triagem Gemma caiu em fail-open (%d dominios viram INVESTIGATE)",
            len(batch),
        )
    else:
        telemetry.increment_counter("gemma_triage_cost_usd_total", amount=outcome.cost_usd)
        _bump_batch("gemma_triage_cost_usd_total", amount=outcome.cost_usd)

    for item in batch:
        lookup_key = normalize_domain(item.domain)
        result = outcome.results.get(lookup_key)
        if result is None:
            logger.error(
                "Sem resultado de triagem para %s (chave %s ausente na resposta) -- fail-open",
                item.domain,
                lookup_key,
            )
            result = TriageResult(
                domain=lookup_key,
                verdict="INVESTIGATE",
                risk_score=0.5,
                target_brand=item.assessment.matched_brand,
                rationale="Fail-open: resultado ausente na resposta em lote do Gemma",
            )

        token = otel_context.attach(item.otel_context)
        try:
            with tracer.start_as_current_span("gemma.triage") as span:
                span.set_attribute("gemma.model_id", outcome.model_id)
                span.set_attribute("gemma.latency_ms", latency_share)
                span.set_attribute("gemma.estimated_cost_usd", cost_share)
                span.set_attribute("gemma.verdict", result.verdict)
                span.set_attribute("gemma.risk_score", result.risk_score)
                span.set_attribute("gemma.fallback_used", outcome.fallback_used)

            telemetry.increment_counter("gemma_triage_total")
            _bump_batch("gemma_triage_total")
            if outcome.fallback_used:
                telemetry.increment_counter("gemma_fallback_total")
                _bump_batch("gemma_fallback_total")

            if result.verdict == "DISCARD":
                telemetry.increment_counter("gemma_discarded_total")
                _bump_batch("gemma_discarded_total")
                logger.info(
                    "GEMMA DISCARD domain=%s risk=%.2f rationale=%s",
                    item.domain,
                    result.risk_score,
                    result.rationale,
                )
                await asyncio.to_thread(_record_triage_discard, item.domain, item.assessment, result)
            else:
                if result.verdict == "ESCALATE_IMMEDIATE":
                    telemetry.increment_counter("gemma_escalated_total")
                    _bump_batch("gemma_escalated_total")
                    logger.warning(
                        "GEMMA ESCALATE_IMMEDIATE domain=%s risk=%.2f rationale=%s",
                        item.domain,
                        result.risk_score,
                        result.rationale,
                    )
                with tracer.start_as_current_span("pubsub.publish") as pub_span:
                    pub_span.set_attribute("pubsub.topic", settings.suspicious_topic_id)
                    pub_span.set_attribute("pubsub.priority", result.verdict)
                    _publish_suspicious_domain(
                        item.domain,
                        item.assessment.matched_brand,
                        item.assessment.score,
                        priority=result.verdict,
                    )
        finally:
            otel_context.detach(token)


async def _triage_batch_timer() -> None:
    """Garante que um lote parcial nao fique parado indefinidamente
    esperando chegar a `gemma_batch_max_size` -- flush no maximo a cada
    `gemma_batch_window_seconds` (CT log entrega em rajada, nem sempre
    enche o lote rapido)."""
    while True:
        await asyncio.sleep(settings.gemma_batch_window_seconds)
        if _pending_batch:
            await _flush_triage_batch()


# Timestamp (`time.monotonic()`) do momento em que a conexao caiu -- `None`
# enquanto conectado. Usado so para medir a DURACAO da lacuna de cobertura
# (item explicito do pedido: certstream nao tem replay, todo evento emitido
# durante a lacuna e PERDIDO -- vira limitacao honesta no FINDINGS, nunca
# escondida). So um worker de ct-listener roda por processo, entao uma
# variavel de modulo (sem lock) e suficiente -- os callbacks abaixo rodam
# todos na MESMA thread (a thread onde `_run_certstream_once` bloqueia em
# `run_forever`), nunca concorrentes entre si.
_last_disconnect_monotonic: float | None = None


def _on_certstream_open() -> None:
    """Callback real da lib (`certstream.core.CertStreamClient.__init__`,
    parametro `on_open`) -- dispara quando o handshake do websocket
    termina. Se havia uma lacuna aberta (`_last_disconnect_monotonic` != None),
    essa e a reconexao que a fecha: mede e loga a duracao exata."""
    global _last_disconnect_monotonic
    if _last_disconnect_monotonic is None:
        logger.info("certstream conectado")
        return
    gap_seconds = max(time.monotonic() - _last_disconnect_monotonic, 0.0)
    logger.warning(
        "certstream RECONECTADO apos lacuna de %.1fs sem cobertura -- "
        "certstream nao tem replay, eventos emitidos durante a lacuna foram PERDIDOS "
        "(limitacao honesta de cobertura, ver FINDINGS.md)",
        gap_seconds,
    )
    observation_run.bump({"websocket_disconnects_total": 1, "websocket_gap_seconds_total": gap_seconds})
    _last_disconnect_monotonic = None


def _on_certstream_error(exc: BaseException) -> None:
    """Callback real da lib (parametro `on_error`) -- dispara em QUALQUER
    erro da conexao websocket, inclusive os que a lib usada a esconder
    dentro do proprio loop de retry interno (ver docstring de
    `_run_certstream_once` abaixo). Marca o INICIO da lacuna, se ainda nao
    marcado (varios erros podem disparar antes da lib desistir de vez)."""
    global _last_disconnect_monotonic
    if _last_disconnect_monotonic is None:
        _last_disconnect_monotonic = time.monotonic()
    logger.warning("Erro na conexao certstream: %s", exc)


def _run_certstream_once() -> None:
    """UMA tentativa de conexao -- ao contrario de
    `certstream.listen_for_events` (a funcao publica do pacote), que tem
    seu PROPRIO `while True: ... time.sleep(5)` interno (verificado lendo
    `certstream/core.py` da versao instalada/pinada, 1.12 -- ver
    requirements.txt): toda reconexao comum acontecia DENTRO dessa funcao,
    com um delay fixo de 5s, nunca retornando para este processo. Isso
    tornava o backoff exponencial deste modulo teatro -- so seria
    exercitado por uma excecao que escapasse do `while True` da lib
    (raro), nunca por uma queda comum de websocket. Chamar
    `CertStreamClient` diretamente (classe interna do pacote, nao API
    publica documentada -- por isso a versao esta PINADA em
    requirements.txt) devolve o controle de reconexao/backoff para
    `run_listener_with_reconnect`, e os callbacks `on_open`/`on_error`
    (esses sim, parametros publicos e documentados) dao visibilidade real
    de quando a conexao cai e quando volta."""
    client = CertStreamClient(
        handle_certstream_message,
        CERTSTREAM_URL,
        on_open=_on_certstream_open,
        on_error=_on_certstream_error,
    )
    client.run_forever(ping_interval=15)


async def run_listener_with_reconnect() -> None:
    global _last_disconnect_monotonic
    delay = _RECONNECT_MIN_DELAY_SECONDS
    while True:
        try:
            logger.info("Conectando ao certstream em %s", CERTSTREAM_URL)
            await asyncio.to_thread(_run_certstream_once)
            logger.warning("Conexao com certstream encerrada")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Erro na conexao com certstream")

        if _last_disconnect_monotonic is None:
            # run_forever() retornou sem passar por _on_certstream_error
            # (ex: fechamento limpo do lado do servidor, sem erro) -- marca
            # a lacuna a partir de AGORA mesmo assim, para nao perder o
            # dado (a proxima reconexao ainda mede e loga a duracao certa).
            _last_disconnect_monotonic = time.monotonic()

        await asyncio.to_thread(_flush_batch)
        logger.info("Reconectando em %ds (backoff exponencial)", delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, _RECONNECT_MAX_DELAY_SECONDS)


async def main() -> None:
    global _loop
    _loop = asyncio.get_running_loop()
    try:
        await asyncio.gather(
            run_listener_with_reconnect(), _triage_batch_timer(), observation_run.checkpoint_loop()
        )
    except KeyboardInterrupt:
        logger.info("Encerrado pelo usuario")
    finally:
        await _flush_triage_batch()
        await asyncio.to_thread(_flush_batch)
        _publisher.transport.close()


if __name__ == "__main__":
    asyncio.run(main())
