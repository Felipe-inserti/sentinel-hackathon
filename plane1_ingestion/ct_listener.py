"""Plano 1 - Ingestao (Certificate Transparency Listener).

Le o Certificate Transparency via POLLING RFC 6962 (get-sth/get-entries)
contra UM log (Argon2026h2, ver `config.py::ct_log_base_url`), roda cada
dominio recem-emitido pelo `prefilter` (matematica pura, zero LLM) e, para
os sobreviventes, agrupa em lotes para a triagem intermediaria do Gemma
(`gemma_triage.py`) antes de publicar no Pub/Sub para o Plano 2 (scraping +
Gemini, caro). Cascata de tres niveis, cada estagio mais caro e mais raro
que o anterior:

    prefiltro (matematica, custo zero)
      -> Gemma 3 270M em lote (CPU, self-hosted, quase zero)
        -> Gemini + scraping (Plano 2, caro)

## Por que RFC 6962 em vez do certstream (websocket de terceiro)

O certstream (`wss://certstream.calidog.io`) ficou fora do ar e nunca teve
replay -- toda lacuna de conexao era dado perdido para sempre (limitacao
documentada nas sprints anteriores). Ler o log diretamente via
get-sth/get-entries elimina essa lacuna: o cursor (indice da ultima entrada
lida) e persistido (`observation_run.save_ct_cursor`, quando um run de
observacao esta ativo) e retomado no restart -- uma queda vira um atraso
temporario, nunca uma perda permanente. O parsing binario (`leaf_input`/
`extra_data`, decisao x509_entry vs precert_entry) fica isolado em
`plane1_ingestion/ct_rfc6962.py` -- este modulo so consome
`ParsedCertEntry` ja pronto.

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
import time
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from google.api_core.exceptions import GoogleAPICallError
from google.cloud import firestore, pubsub_v1
from opentelemetry import context as otel_context
from opentelemetry.context import Context

import observation_run
import telemetry
from config import settings
from gemma_triage import DomainSignals, TriageResult, triage_batch
from plane1_ingestion import ct_rfc6962
from plane1_ingestion.prefilter import DomainRiskAssessment, analyze_domain, normalize_domain

# `telemetry.setup()` configura o logging JSON estruturado no logger raiz
# (`configure_json_logging`) -- todo `logging.getLogger(...)` daqui em
# diante ja sai formatado e correlacionado com o span ativo.
tracer = telemetry.setup("sentinel-ct-listener")
logger = logging.getLogger("ct_listener")

# Espelhamento em lote das metricas no Firestore: escrever a cada dominio
# seria muito volume (~736k entradas/hora medidas ao vivo contra
# Argon2026h2, ~99% descartadas). Acumula localmente e grava um unico
# documento a cada N -- o cursor de polling (ver `run_polling_loop`)
# tambem e persistido junto, na MESMA cadencia (`_flush_batch`).
_METRICS_FLUSH_INTERVAL = 500

_publisher = pubsub_v1.PublisherClient()
_topic_path = _publisher.topic_path(settings.gcp_project_id, settings.suspicious_topic_id)
_firestore_db = firestore.Client()

_processed_count = 0
_suspicious_count = 0
_batch_deltas: dict[str, int] = {}

# Fila de sobreviventes do prefiltro aguardando triagem em lote pelo Gemma.
# Ao contrario do certstream (callback sincrono numa thread separada da
# lib, exigindo `asyncio.run_coroutine_threadsafe`), o polling RFC 6962
# roda inteiramente DENTRO do event loop asyncio (via `asyncio.to_thread`
# so nas chamadas HTTP bloqueantes) -- entao esta fila e manipulada sempre
# do mesmo loop, sem cruzar threads.
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
        "source": "ct_rfc6962",
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


async def _process_certificate_entry(entry: ct_rfc6962.ParsedCertEntry) -> None:
    """Processa UMA entrada ja decodificada do log CT -- equivalente ao
    corpo do loop de dominios que antes vivia dentro do callback do
    certstream (`handle_certstream_message`), com o MESMO pipeline
    downstream (prefiltro -> fila de triagem Gemma -> Pub/Sub) intacto. So
    a fonte do evento mudou -- ver docstring do modulo."""
    global _processed_count, _suspicious_count

    for domain in entry.domains:
        _processed_count += 1

        with tracer.start_as_current_span("ct.ingest") as ingest_span:
            ingest_span.set_attribute("domain", domain)
            ingest_span.set_attribute("ct.log_index", entry.log_index)
            ingest_span.set_attribute("ct.entry_type", entry.entry_type)
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
                certificate_age_seconds=entry.certificate_age_seconds,
                otel_context=otel_context.get_current(),
            )
            await _enqueue_for_triage(item)

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
    `gemma_batch_window_seconds` (o log entrega em rajada por pagina de
    get-entries, nem sempre enche o lote rapido)."""
    while True:
        await asyncio.sleep(settings.gemma_batch_window_seconds)
        if _pending_batch:
            await _flush_triage_batch()


@dataclass
class _ConcurrencyController:
    """Sobe a concorrencia de faixas simultaneas gradualmente enquanto nada
    da errado, e reduz assim que UM 429 aparece em qualquer faixa de
    QUALQUER rodada -- pedido explicito: "suba gradualmente e PARE no
    primeiro 429, ser bloqueado pelo log e pior que ficar atrasado". Vive
    so pela duracao do processo (reinicia em `ct_fetch_concurrency_min` a
    cada boot -- nunca carrega concorrencia agressiva de uma execucao
    anterior para uma nova, mesmo espirito conservador do resto do
    modulo)."""

    current: int
    minimum: int
    maximum: int

    def note_round_result(self, rate_limited: bool) -> None:
        if rate_limited:
            reduced = max(self.minimum, self.current // 2)
            if reduced != self.current:
                logger.warning(
                    "429 recebido -- reduzindo concorrencia de %d para %d faixas simultaneas",
                    self.current,
                    reduced,
                )
            self.current = reduced
        elif self.current < self.maximum:
            self.current += 1
            logger.info("Rodada sem erro -- subindo concorrencia para %d faixas simultaneas", self.current)


def _plan_windows(cursor: int, tree_size: int, concurrency: int, window_span: int) -> list[tuple[int, int]]:
    """Particiona `[cursor, tree_size-1]` em ate `concurrency` faixas
    contiguas e NAO sobrepostas, cada uma com ate `window_span` indices.
    start/end de get-entries sao independentes (RFC 6962), entao cada faixa
    vira uma tarefa asyncio propria -- ver `_drain_window`."""
    windows: list[tuple[int, int]] = []
    pos = cursor
    last_index = tree_size - 1
    for _ in range(concurrency):
        if pos > last_index:
            break
        end = min(pos + window_span - 1, last_index)
        windows.append((pos, end))
        pos = end + 1
    return windows


async def _drain_window(start: int, end: int, rate_limited_flag: list[bool]) -> None:
    """Le exaustivamente UMA faixa `[start, end]`, do jeito sequencial de
    sempre (pede, recebe menos do que pediu, avanca pelo REAL devolvido,
    repete) ate esgotar a faixa inteira. Erro transitorio nunca "pula" a
    faixa -- so tenta de novo com backoff, sempre a partir de onde parou
    (`pos`, nao `start`) -- e exatamente o requisito "uma faixa que falha
    nao pode ser pulada silenciosamente". Em 429, marca
    `rate_limited_flag[0] = True` (lista de 1 elemento so para ter uma
    referencia mutavel compartilhada entre as N tarefas concorrentes da
    mesma rodada, ver `_run_parallel_round`) e tambem faz backoff -- a
    faixa AINDA precisa terminar, so a rodada seguinte que vai rodar com
    menos concorrencia.

    Seguranca de concorrencia: multiplas faixas chamam
    `_process_certificate_entry`/`_enqueue_for_triage` ao mesmo tempo, mas
    tudo roda no MESMO event loop (nao threads) -- os incrementos de
    contador e o swap de `_pending_batch` em `_flush_triage_batch`
    acontecem inteiramente entre dois `await`, entao o proprio
    escalonamento cooperativo do asyncio impede uma corrida (nenhuma outra
    tarefa roda no meio de uma secao sem `await`)."""
    pos = start
    backoff = settings.ct_http_backoff_min_seconds
    while pos <= end:
        try:
            raw_entries = await asyncio.to_thread(ct_rfc6962.fetch_entries, pos, end)
        except ct_rfc6962.CTLogRateLimitedError:
            rate_limited_flag[0] = True
            logger.warning(
                "429 na faixa [%d, %d] (parado em %d) -- backoff %.1fs", start, end, pos, backoff
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, settings.ct_http_backoff_max_seconds)
            continue
        except ct_rfc6962.CTLogUnavailableError:
            logger.warning(
                "erro transitorio na faixa [%d, %d] (parado em %d) -- backoff %.1fs",
                start,
                end,
                pos,
                backoff,
                exc_info=True,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, settings.ct_http_backoff_max_seconds)
            continue

        backoff = settings.ct_http_backoff_min_seconds

        if not raw_entries:
            # pos <= end (ainda deveria haver dado), mas o log devolveu
            # vazio -- timing/merge delay, nao erro. Espera um pouco e
            # tenta nesta MESMA posicao de novo.
            await asyncio.sleep(settings.ct_poll_interval_seconds)
            continue

        for offset, raw_entry in enumerate(raw_entries):
            index = pos + offset
            entry = ct_rfc6962.parse_leaf_entry(
                index, raw_entry.get("leaf_input", ""), raw_entry.get("extra_data", "")
            )
            if entry is not None:
                await _process_certificate_entry(entry)

        pos += len(raw_entries)


async def _run_parallel_round(cursor: int, tree_size: int, controller: _ConcurrencyController) -> int:
    """UMA rodada: particiona `[cursor, tree_size-1]` em ate
    `controller.current` faixas e espera TODAS terminarem antes de
    persistir qualquer cursor novo -- e assim que a garantia pedida e
    cumprida: "so avanca o cursor persistido ate onde TODAS as faixas
    anteriores completaram". Como nada e persistido ate `asyncio.gather`
    devolver (ou seja, ate a ULTIMA faixa da rodada terminar, seja qual for
    a ordem em que cada uma individualmente terminou), uma faixa mais
    lenta ou que precisou de varios retries nunca e "ultrapassada" por uma
    faixa mais rapida de indice maior -- a rodada so avanca em bloco."""
    windows = _plan_windows(cursor, tree_size, controller.current, settings.ct_get_entries_request_size)
    if not windows:
        return cursor

    rate_limited_flag = [False]
    await asyncio.gather(*(_drain_window(start, end, rate_limited_flag) for start, end in windows))

    new_cursor = windows[-1][1] + 1
    controller.note_round_result(rate_limited_flag[0])
    observation_run.save_ct_cursor(new_cursor)
    return new_cursor


async def run_polling_loop() -> None:
    """Loop principal de ingestao -- substitui a conexao websocket do
    certstream por polling RFC 6962 (get-sth/get-entries) contra
    `settings.ct_log_base_url` (Argon2026h2), com ATE
    `settings.ct_fetch_concurrency_max` faixas de indice lidas em paralelo
    por rodada (ver `_run_parallel_round`) -- uma faixa sequencial so
    (~49 entradas/s medidas ao vivo) fica bem abaixo da vazao real do log
    (~204 entradas/s); paralelizar e o unico jeito de alcancar tempo real.

    Cursor: no primeiro boot (sem cursor persistido), comeca do
    `tree_size` ATUAL -- NUNCA do indice 0 (o log tem bilhoes de entradas
    historicas de certificados com validade nesta janela temporal; comecar
    do zero levaria dias so para alcancar o presente). Se
    `observation_run.load_ct_cursor()` devolver um valor (run de
    observacao ativo com cursor de uma execucao anterior), retoma dali --
    e o que torna o job resistente a reinicio/timeout do Cloud Run Job."""
    cursor = observation_run.load_ct_cursor()
    if cursor is None:
        logger.info("Sem cursor persistido -- iniciando do tree_size atual do log (nunca do indice 0)")
        while True:
            try:
                sth = await asyncio.to_thread(ct_rfc6962.fetch_sth)
                break
            except ct_rfc6962.CTLogUnavailableError:
                logger.warning("get-sth inicial falhou, tentando de novo em %.0fs", settings.ct_http_backoff_min_seconds, exc_info=True)
                await asyncio.sleep(settings.ct_http_backoff_min_seconds)
        cursor = sth.tree_size
        logger.info("Cursor inicial = %d", cursor)
    else:
        logger.info("Retomando polling a partir do cursor persistido: %d", cursor)

    controller = _ConcurrencyController(
        current=settings.ct_fetch_concurrency_min,
        minimum=settings.ct_fetch_concurrency_min,
        maximum=settings.ct_fetch_concurrency_max,
    )

    backoff = settings.ct_http_backoff_min_seconds
    while True:
        try:
            sth = await asyncio.to_thread(ct_rfc6962.fetch_sth)
        except ct_rfc6962.CTLogUnavailableError:
            logger.warning("get-sth falhou, backoff %.1fs", backoff, exc_info=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, settings.ct_http_backoff_max_seconds)
            continue
        backoff = settings.ct_http_backoff_min_seconds

        if cursor >= sth.tree_size:
            # Em dia com o log -- espera o ritmo normal de poll, isto NAO
            # e backoff de erro.
            await asyncio.sleep(settings.ct_poll_interval_seconds)
            continue

        cursor = await _run_parallel_round(cursor, sth.tree_size, controller)


async def main() -> None:
    try:
        await asyncio.gather(
            run_polling_loop(), _triage_batch_timer(), observation_run.checkpoint_loop()
        )
    except KeyboardInterrupt:
        logger.info("Encerrado pelo usuario")
    finally:
        await _flush_triage_batch()
        await asyncio.to_thread(_flush_batch)
        _publisher.transport.close()


if __name__ == "__main__":
    asyncio.run(main())
