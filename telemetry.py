"""Observabilidade do Sentinel (OpenTelemetry) -- traces e metricas ponta a
ponta, exportados via a Telemetry API nativa do Google Cloud (OTLP/gRPC).

## Por que OTLP nativo, nao os pacotes `opentelemetry-exporter-gcp-*`

Verifiquei a documentacao oficial (nao adivinhado): os pacotes
`opentelemetry-exporter-gcp-trace` e `opentelemetry-exporter-gcp-monitoring`
estao DEPRECADOS -- o proprio PyPI de ambos traz o aviso "Google Cloud
supports native OpenTelemetry Protocol (OTLP) ingestion for Cloud Trace,
Cloud Monitoring, and Cloud Logging via the Telemetry API." O caminho
recomendado (confirmado no guia de migracao do repositorio
GoogleCloudPlatform/opentelemetry-operations-python) e apontar o
`OTLPSpanExporter`/`OTLPMetricExporter` padrao do OTel para
`telemetry.googleapis.com`, autenticando via ADC (`google.auth.default()`)
composto com credenciais de canal gRPC. Confirmei essa API de verdade
(assinaturas de `OTLPSpanExporter`, `AuthMetadataPlugin`, etc.) contra os
pacotes instalados neste ambiente antes de escrever este modulo.

## Desativacao local (requisito: testes locais nao devem tentar exportar)

`settings.otel_enabled=False` (env `OTEL_ENABLED=false`) pula a construcao
do exportador OTLP inteira -- tracer/meter continuam existindo (API do
OTel sempre funciona, mesmo sem processor/reader anexado), so que sem
nenhum destino de rede, entao nenhum I/O acontece e o overhead e
essencialmente zero. Mesmo com OTEL_ENABLED=true, se a construcao do canal
gRPC autenticado falhar (ex: sem ADC configurado neste sandbox), o setup
loga o erro e segue sem exportar em vez de derrubar o processo -- span
sempre e criado, exportar e best-effort.

## Por que os contadores tambem gravam no Firestore

`estimated_cost_saved_usd_total` (o numero do pitch) so pode ser calculado
combinando o total descartado pelo prefiltro (Plano 1, processo separado)
com o custo medio real por chamada de LLM (Plano 2, outro processo
separado) -- os dois nunca compartilham memoria. Cloud Monitoring teria
essa informacao combinada, mas consultar de volta por API teria atraso de
ingestao e exigiria mais uma dependencia/IAM role que nao consigo validar
neste ambiente. Firestore ja e infraestrutura existente do projeto, com
leitura imediata -- ideal para `metrics_report.py` numa demo ao vivo. Os
contadores OTel continuam sendo a fonte "oficial" exportada para Cloud
Monitoring, cumprindo o requisito de compatibilidade da trilha.

## Nao bloqueia o event loop

`Counter.add()` so escreve em memoria (o SDK do OTel exporta em lote, numa
thread de fundo, via `BatchSpanProcessor`/`PeriodicExportingMetricReader`
-- nunca de forma sincrona na chamada). O unico I/O sincrono deste modulo e
`flush_metrics_to_firestore`, que deve ser chamado de dentro de
`asyncio.to_thread(...)` pelos chamadores (como ja fazem com os outros
acessos a Firestore no projeto).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

from google.cloud import firestore
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from config import settings

_OTLP_ENDPOINT = "telemetry.googleapis.com"
_METRICS_DOCUMENT = "pipeline_totals"

# Nomes exatos exigidos: alimentam Cloud Monitoring (via OTel) e o
# documento compartilhado no Firestore lido por metrics_report.py.
_COUNTER_NAMES = (
    "certificates_ingested_total",
    "certificates_discarded_by_prefilter_total",
    "llm_invocations_total",
    "cache_hits_total",
    "tokens_consumed_total",
    "estimated_cost_usd_total",
    # Camada de triagem Gemma (ver gemma_triage.py). `gemma_triage_cost_usd_total`
    # nao estava na lista literal do requisito, mas sem ele o funil de custo
    # do metrics_report.py (requisito separado) ficaria incompleto -- o
    # unico dos novos contadores que da pra rastrear ao vivo (nao depende
    # de dado de outro processo, ao contrario de cost_saved_by_gemma, que
    # fica derivado no relatorio pelo mesmo motivo de estimated_cost_saved_usd_total).
    "gemma_triage_total",
    "gemma_discarded_total",
    "gemma_escalated_total",
    "gemma_fallback_total",
    "gemma_triage_cost_usd_total",
    # evidence_agent.py (Sprint 4) -- coleta e determinística/zero-LLM, entao
    # nao ha contador de custo aqui; so o par completo/parcial, para
    # observabilidade de quantos dossies chegam com o bundle incompleto
    # (ex: site saiu do ar entre a investigacao e a coleta de evidencia).
    "evidence_bundles_collected_total",
    "evidence_bundles_partial_total",
    # takedown_agent.py (Sprint 6) -- executado conta uma acao completa
    # (>=0 canais notificados em DRY_RUN); rejeitado cobre TODAS as
    # recusas de seguranca (sem aprovacao, allowlist, rate limit,
    # DRY_RUN=false sem suporte) -- ver takedown_agent.py::process_takedown_approval.
    "takedown_actions_executed_total",
    "takedown_actions_rejected_total",
    # brand_memory.py (Sprint 7, Parte B) -- few-shot injetado por marca.
    # O trade-off explicito pedido no sprint: few-shot melhora precisao mas
    # aumenta tokens de ENTRADA por investigacao, contra a tese de token
    # economy (ver CLAUDE.md) -- estes contadores tornam esse custo
    # visivel em vez de escondido. Estimativa por heuristica de
    # caracteres/token (ver brand_memory.estimate_extra_tokens), nao
    # medicao real de tokenizador -- mesma disciplina de aproximacao
    # documentada em config.py para o preco do Cloud Run.
    "brand_memory_examples_injected_total",
    "brand_memory_estimated_extra_tokens_total",
    "brand_memory_estimated_extra_cost_usd_total",
    # Etapa C (observation_run.py) -- so os que fazem sentido como contador
    # OTel/Cloud Monitoring GLOBAL (vida inteira do projeto); os contadores
    # ESCOPADOS por run de observacao vivem em `observation_runs/{run_id}`
    # (Firestore, ver observation_run.py), nao aqui.
    "malicious_confirmed_total",
    "websocket_disconnects_total",
    "websocket_gap_seconds_total",
)

_propagator = TraceContextTextMapPropagator()
_logger = logging.getLogger("telemetry")

_tracer: trace.Tracer | None = None
_counters: dict[str, metrics.Counter] = {}
_metrics_db: firestore.Client | None = None


def _build_gcp_channel_credentials():
    import google.auth
    import google.auth.transport.grpc
    import google.auth.transport.requests
    import grpc
    from google.auth.transport.grpc import AuthMetadataPlugin

    credentials, _ = google.auth.default()
    request = google.auth.transport.requests.Request()
    auth_metadata_plugin = AuthMetadataPlugin(credentials=credentials, request=request)
    return grpc.composite_channel_credentials(
        grpc.ssl_channel_credentials(),
        grpc.metadata_call_credentials(auth_metadata_plugin),
    )


def _try_build_span_processor() -> BatchSpanProcessor | None:
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter(
            endpoint=_OTLP_ENDPOINT, credentials=_build_gcp_channel_credentials()
        )
        return BatchSpanProcessor(exporter)
    except Exception:
        _logger.exception(
            "Falha ao configurar exportador OTLP de traces para %s -- spans "
            "continuam sendo criados, so nao saem para o Cloud Trace",
            _OTLP_ENDPOINT,
        )
        return None


def _try_build_metric_reader() -> PeriodicExportingMetricReader | None:
    try:
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

        exporter = OTLPMetricExporter(
            endpoint=_OTLP_ENDPOINT, credentials=_build_gcp_channel_credentials()
        )
        return PeriodicExportingMetricReader(exporter, export_interval_millis=15_000)
    except Exception:
        _logger.exception(
            "Falha ao configurar exportador OTLP de metricas para %s -- "
            "contadores continuam funcionando em memoria/Firestore, so nao "
            "saem para o Cloud Monitoring",
            _OTLP_ENDPOINT,
        )
        return None


def setup(service_name: str) -> trace.Tracer:
    """Chamar uma vez por processo (ct_listener.py, orchestrator.py), o
    mais cedo possivel na inicializacao. Configura tracer, meter, os
    contadores e o logging JSON estruturado; devolve o tracer para uso do
    chamador (`increment_counter`/`flush_metrics_to_firestore` sao
    funcoes de modulo, nao precisam do retorno)."""
    global _tracer, _counters, _metrics_db

    # O backend do Telemetry API rejeita a chamada sem "gcp.project_id" no
    # Resource ("Resource is missing required attribute \"gcp.project_id\"",
    # confirmado testando contra o endpoint real -- `cloud.account.id`, a
    # convencao semantica "padrao" do OTel que o GoogleCloudResourceDetector
    # preencheria automaticamente rodando em infra GCP de verdade, NAO
    # satisfaz essa checagem; o backend quer literalmente essa chave).
    # Setamos os dois na mao (garantido, funciona fora de GCP tambem) e
    # ainda mesclamos o que o detector real encontrar -- em Cloud Run/GKE
    # ele adiciona atributos extras (regiao, plataforma, revisao do
    # servico) via o metadata server, que fora de GCP (aqui, local) vem
    # vazio e nao atrapalha.
    try:
        from opentelemetry.resourcedetector.gcp_resource_detector import (
            GoogleCloudResourceDetector,
        )

        detected_resource = GoogleCloudResourceDetector().detect()
    except Exception:
        _logger.exception("Deteccao de recurso GCP falhou, seguindo so com os atributos manuais")
        detected_resource = Resource.get_empty()

    resource = detected_resource.merge(
        Resource.create(
            {
                "service.name": service_name,
                "cloud.account.id": settings.gcp_project_id,
                "gcp.project_id": settings.gcp_project_id,
                # `cloud.region`, NAO `settings.gcp_location` -- reproduzido
                # contra o backend real (Sprint 8, Parte B): sem um
                # `cloud.region` valido no Resource, o ingest de METRICAS da
                # Telemetry API (traces exportam OK sem isso) rejeita com
                # "write for resource failed: Unrecognized region or
                # location". `gcp_location` vale "global" neste projeto
                # (endpoint do Vertex AI) -- "global" nao e uma regiao que o
                # Cloud Monitoring reconhece aqui, por isso uma variavel
                # separada (`settings.otel_region`, ver config.py).
                "cloud.region": settings.otel_region,
            }
        )
    )

    tracer_provider = TracerProvider(resource=resource)
    meter_readers = []
    if settings.otel_enabled:
        span_processor = _try_build_span_processor()
        if span_processor is not None:
            tracer_provider.add_span_processor(span_processor)
        metric_reader = _try_build_metric_reader()
        if metric_reader is not None:
            meter_readers.append(metric_reader)

    trace.set_tracer_provider(tracer_provider)
    meter_provider = MeterProvider(resource=resource, metric_readers=meter_readers)
    metrics.set_meter_provider(meter_provider)

    _tracer = trace.get_tracer(service_name)
    meter = metrics.get_meter(service_name)
    _counters = {
        name: meter.create_counter(name, description=name.replace("_", " "))
        for name in _COUNTER_NAMES
    }
    _metrics_db = firestore.Client()

    configure_json_logging()
    _logger.info(
        "Telemetria inicializada para %s (otel_enabled=%s)", service_name, settings.otel_enabled
    )
    return _tracer


def get_tracer() -> trace.Tracer:
    if _tracer is None:
        raise RuntimeError("telemetry.setup(service_name) precisa ser chamado antes de get_tracer()")
    return _tracer


def increment_counter(name: str, amount: int | float = 1, **attributes: Any) -> None:
    """Incrementa o contador OTel em memoria -- nao bloqueia (exportado em
    lote por uma thread de fundo do SDK). NAO grava no Firestore -- use
    `flush_metrics_to_firestore` para isso, em lote."""
    counter = _counters.get(name)
    if counter is None:
        _logger.warning("Contador desconhecido: %s", name)
        return
    counter.add(amount, attributes=attributes or None)


def flush_metrics_to_firestore(deltas: dict[str, int | float]) -> None:
    """Grava um lote de incrementos no documento compartilhado
    `metrics/pipeline_totals` -- unica fonte que Plano 1 e Plano 2 (dois
    processos, sem memoria compartilhada) enxergam em comum. Chamada
    SINCRONA/bloqueante: o chamador deve envolver em
    `asyncio.to_thread(...)` se estiver num event loop."""
    if not deltas or _metrics_db is None:
        return
    doc_ref = _metrics_db.collection(settings.metrics_firestore_collection).document(_METRICS_DOCUMENT)
    doc_ref.set({k: firestore.Increment(v) for k, v in deltas.items()}, merge=True)


def estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    """Estimativa de custo em USD com base nos precos configurados (ver
    `config.settings.gemini_*_price_per_million_usd`). Nunca chama nenhuma
    API de billing -- e so aritmetica sobre tokens ja medidos."""
    return (
        (input_tokens / 1_000_000) * settings.gemini_input_price_per_million_usd
        + (output_tokens / 1_000_000) * settings.gemini_output_price_per_million_usd
    )


def inject_traceparent(carrier: dict[str, str]) -> dict[str, str]:
    """Injeta `traceparent` (W3C Trace Context) no dict `carrier` -- usar
    como atributos da mensagem ao publicar no Pub/Sub, para o trace
    atravessar o limite entre servicos."""
    _propagator.inject(carrier)
    return carrier


def extract_context(carrier: dict[str, str] | Any):
    """Extrai o contexto de trace de um `traceparent` recebido (ex:
    `message.attributes` do Pub/Sub). Devolve um `Context` do OTel --
    `opentelemetry.context.attach(...)` esse valor antes de abrir spans
    para continuar o mesmo trace."""
    return _propagator.extract(carrier)


# --- Logging JSON estruturado, correlacionado com trace/span ativos -------

_GCP_TRACE_KEY = "logging.googleapis.com/trace"
_GCP_SPAN_KEY = "logging.googleapis.com/spanId"


class _JsonTraceFormatter(logging.Formatter):
    """Formata cada log como uma linha JSON. Se houver um span OTel ativo
    no momento do log, inclui `logging.googleapis.com/trace` (formato
    `projects/{id}/traces/{trace_id}`) e `.../spanId` -- os nomes de campo
    exatos que o Cloud Logging promove automaticamente para vincular o log
    ao trace na UI (verificado na doc oficial, nao adivinhado)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            trace_id_hex = format(span_context.trace_id, "032x")
            payload[_GCP_TRACE_KEY] = f"projects/{settings.gcp_project_id}/traces/{trace_id_hex}"
            payload[_GCP_SPAN_KEY] = format(span_context.span_id, "016x")

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False)


def configure_json_logging() -> None:
    root = logging.getLogger()
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonTraceFormatter())
    root.handlers = [handler]
