"""Etapa C -- instrumentacao do run de observacao de 48h contra o
Certificate Transparency real (teto de orcamento: US$25, ver
infra/variables.tf::monthly_budget_usd e infra/observation_scheduler.tf).

Sem esta camada, o run acontece e nao sobra nada verificavel para o
FINDINGS.md nem para os numeros do video -- este modulo existe so para
tornar esse dado honesto e acumulavel.

## Por que tudo aqui e no-op por padrao

So ativa quando `settings.observation_run_id` esta configurado (sem
default de proposito, ver config.py -- mesma disciplina de
`gemini_model_id`). Fora de uma observacao deliberada (dev, teste, CI,
demo curta), TODA funcao deste modulo devolve imediatamente sem tocar
Firestore -- os 214 testes anteriores a esta etapa continuam passando sem
setar nenhuma env var nova.

## `observation_runs/{run_id}` -- por que sobrevive a re-disparo

O Cloud Scheduler (`infra/observation_scheduler.tf`) relanca os 4 Cloud Run
Jobs varias vezes ao longo das 48h -- cada execucao e um PROCESSO NOVO, sem
memoria do anterior. `bump()` grava com `firestore.Increment` (mesmo
mecanismo ja usado por `telemetry.flush_metrics_to_firestore`), que soma no
documento EXISTENTE em vez de sobrescrever -- desde que todo processo
compartilhe o MESMO `OBSERVATION_RUN_ID` (configuracao do operador, nao
gerado em runtime: um valor gerado por processo quebraria exatamente a
garantia de "resistente a re-disparo" pedida), os contadores nunca zeram.

## Guarda de custo -- circuit breaker, nao estimativa preditiva

`cost_guard_allows_llm_call()` le o custo JA acumulado (Gemini + Gemma) do
documento do run e recusa quando esse total ja bateu ou passou o teto
(`settings.observation_cost_guard_usd_limit`). Isso aceita uma pequena
ultrapassagem -- a propria chamada que faz o total cruzar o teto ja foi
gasta antes de sabermos o novo total -- documentado aqui, nao escondido:
e uma rede de seguranca contra um bug de cascata (prefiltro/Gemma deixando
passar volume muito acima do esperado), nao uma trava matematicamente
exata de "nunca gastar 1 centavo a mais".
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from google.cloud import firestore

from config import settings

logger = logging.getLogger("observation_run")

_db: firestore.Client | None = None

# True depois da PRIMEIRA tentativa (bem-sucedida ou nao) de garantir
# `started_at` neste PROCESSO -- so uma otimizacao para nao rodar uma
# transacao extra a cada `bump()` (que roda em lote, a cada ~500 eventos,
# ver ct_listener.py::_flush_batch); a correcao entre processos diferentes
# (varias execucoes do Scheduler tentando ao mesmo tempo) vem da transacao
# em si (`_ensure_started_at`), nao desta flag.
_started_at_checked = False

# Nomes dos dois contadores de custo por camada usados pela guarda (ver
# telemetry._COUNTER_NAMES / _flush_batch em ct_listener.py e
# investigate_domain em orchestrator.py -- os MESMOS nomes ja usados la,
# nao uma segunda convencao de nomes).
_GEMINI_COST_FIELD = "estimated_cost_usd_total"
_GEMMA_COST_FIELD = "gemma_triage_cost_usd_total"


def _get_db() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client()
    return _db


def is_active() -> bool:
    """True quando um run de observacao esta configurado
    (`OBSERVATION_RUN_ID` setado). Toda outra funcao deste modulo checa
    isso primeiro e devolve sem I/O quando False."""
    return settings.observation_run_id is not None


def _run_doc_ref() -> firestore.DocumentReference:
    return (
        _get_db()
        .collection(settings.observation_runs_collection)
        .document(settings.observation_run_id)
    )


def _ensure_started_at() -> None:
    """Grava `started_at` UMA VEZ no documento do run -- o inicio real da
    janela de observacao (usado por `observation_report.py` para filtrar
    `investigations` pela janela do run). Transacao (nao um `set(merge=True)`
    direto): varios PROCESSOS diferentes (re-disparos concorrentes do
    Scheduler) podem tentar isso ao mesmo tempo logo no inicio do run --
    "so o primeiro escreve" so e garantido lendo e escrevendo atomicamente."""
    global _started_at_checked
    if _started_at_checked:
        return
    doc_ref = _run_doc_ref()

    @firestore.transactional
    def _txn(transaction: firestore.Transaction) -> None:
        snapshot = doc_ref.get(transaction=transaction)
        data = snapshot.to_dict() if snapshot.exists else None
        if not data or "started_at" not in data:
            transaction.set(doc_ref, {"started_at": datetime.now(timezone.utc)}, merge=True)

    try:
        _txn(_get_db().transaction())
        _started_at_checked = True
    except Exception:
        logger.exception("Falha ao garantir started_at do run %s", settings.observation_run_id)


def bump(deltas: dict[str, int | float]) -> None:
    """Incremento atomico dos contadores acumulados do run corrente
    (`firestore.Increment` por campo -- soma, nunca sobrescreve). No-op se
    nenhum run estiver ativo ou `deltas` estiver vazio. Chamada SINCRONA/
    bloqueante -- envolver em `asyncio.to_thread(...)` a partir de codigo
    async, mesma convencao de `telemetry.flush_metrics_to_firestore`."""
    if not is_active() or not deltas:
        return
    _ensure_started_at()
    try:
        _run_doc_ref().set(
            {
                **{k: firestore.Increment(v) for k, v in deltas.items()},
                "run_id": settings.observation_run_id,
                "last_updated_at": datetime.now(timezone.utc),
            },
            merge=True,
        )
    except Exception:
        logger.exception(
            "Falha ao gravar incremento em observation_runs/%s -- lote %s perdido",
            settings.observation_run_id,
            list(deltas.keys()),
        )


def get_totals() -> dict[str, Any]:
    """Le o documento atual do run -- usado pela guarda de custo, pelo
    alerta de anomalia e por `observation_report.py`. Dict vazio se nenhum
    run estiver ativo ou o documento ainda nao existir (run recem-iniciado,
    nenhum `bump()` gravou ainda)."""
    if not is_active():
        return {}
    try:
        snapshot = _run_doc_ref().get()
    except Exception:
        logger.exception("Falha ao ler observation_runs/%s", settings.observation_run_id)
        return {}
    return snapshot.to_dict() or {} if snapshot.exists else {}


# --- Guarda de custo (item 1 -- prioridade maxima do pedido) ---------------


def cost_guard_allows_llm_call() -> bool:
    """Circuit breaker do gasto acumulado com LLM do RUN INTEIRO (Gemini +
    Gemma), nao por execucao/processo -- ver docstring do modulo. Sempre
    True (permite a chamada) se nenhum run estiver ativo. Loga CRITICAL na
    PRIMEIRA vez que recusa (nao a cada chamada recusada -- evitaria
    inundar o log durante o resto do run) -- ver `_cost_guard_tripped`."""
    if not is_active():
        return True
    totals = get_totals()
    spent = float(totals.get(_GEMINI_COST_FIELD, 0.0) or 0.0) + float(
        totals.get(_GEMMA_COST_FIELD, 0.0) or 0.0
    )
    limit = settings.observation_cost_guard_usd_limit
    if spent >= limit:
        logger.critical(
            "GUARDA DE CUSTO ATIVADA no run %s: gasto acumulado $%.4f >= teto $%.2f -- "
            "Gemini NAO sera mais chamado ate o teto ser aumentado ou o run ser encerrado. "
            "Prefiltro/Gemma continuam rodando normalmente (custo zero/quase zero).",
            settings.observation_run_id,
            spent,
            limit,
        )
        return False
    return True


# --- Checkpoint periodico (item 3) -----------------------------------------


def record_checkpoint() -> None:
    """Snapshot dos contadores acumulados do run, com timestamp, numa
    subcolecao (`observation_runs/{run_id}/checkpoints/{timestamp}`) --
    cada documento e IMUTAVEL (nunca atualizado), formando a serie temporal
    que `observation_report.py` le de volta. No-op se nenhum run estiver
    ativo."""
    if not is_active():
        return
    totals = get_totals()
    checkpoint_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    try:
        _run_doc_ref().collection("checkpoints").document(checkpoint_id).set(
            {**totals, "checkpoint_at": datetime.now(timezone.utc)}
        )
        logger.info("Checkpoint gravado: run=%s id=%s", settings.observation_run_id, checkpoint_id)
    except Exception:
        logger.exception("Falha ao gravar checkpoint do run %s", settings.observation_run_id)


async def checkpoint_loop() -> None:
    """Task de fundo: um checkpoint a cada
    `settings.observation_checkpoint_interval_seconds` (default 15min).
    Retorna IMEDIATAMENTE (sem laco) se nenhum run estiver ativo -- seguro
    chamar incondicionalmente via `asyncio.gather(...)` em todo processo de
    longa duracao (ct_listener.py/orchestrator.py), sem `if` espalhado nos
    pontos de entrada."""
    if not is_active():
        return
    while True:
        await asyncio.sleep(settings.observation_checkpoint_interval_seconds)
        await asyncio.to_thread(record_checkpoint)


# --- Alerta de anomalia (item 5) -------------------------------------------


def check_prefilter_escape_anomaly() -> None:
    """Compara, sobre os totais GLOBAIS do run (nao os contadores locais de
    um processo, que zeram a cada re-disparo do Scheduler), a fracao de
    certificados ingeridos que o prefiltro NAO descartou. Acima do limiar
    configurado (`settings.observation_prefilter_escape_rate_threshold`),
    loga CRITICAL -- descobrir em horas, nao no fim do run, que o
    prefiltro esta deixando passar mais que o esperado (tese do projeto:
    ~99% descartado por matematica pura, ver CLAUDE.md). No-op se nenhum
    run estiver ativo ou a amostra ainda for pequena demais
    (`settings.observation_anomaly_min_sample_size`)."""
    if not is_active():
        return
    totals = get_totals()
    ingested = float(totals.get("certificates_ingested_total", 0) or 0)
    if ingested < settings.observation_anomaly_min_sample_size:
        return
    discarded = float(totals.get("certificates_discarded_by_prefilter_total", 0) or 0)
    escape_rate = 1.0 - (discarded / ingested) if ingested else 0.0
    threshold = settings.observation_prefilter_escape_rate_threshold
    if escape_rate > threshold:
        logger.critical(
            "ANOMALIA no run %s: taxa de escape do prefiltro em %.2f%% (limiar %.2f%%) -- "
            "%d de %d certificados ingeridos NAO foram descartados pelo prefiltro. "
            "Prefiltro pode estar com um bug/regressao de limiar.",
            settings.observation_run_id,
            escape_rate * 100,
            threshold * 100,
            int(ingested - discarded),
            int(ingested),
        )


# --- DRY_RUN travado durante a observacao (item 6) -------------------------


def enforce_dry_run_lock() -> None:
    """Chamar UMA VEZ, na inicializacao de qualquer processo que possa agir
    de verdade no mundo real (hoje, so takedown_agent.py) -- trava
    ADICIONAL, alem da recusa por mensagem que `process_takedown_approval`
    ja fazia antes desta etapa (ver takedown_agent.py). Se um run de
    observacao esta ativo e `settings.dry_run` e False, o processo nem
    chega a se inscrever no Pub/Sub -- coleta e o objetivo do run, acao
    real nao (CLAUDE.md, regra de seguranca #3). No-op (nao levanta nada)
    se nenhum run estiver ativo -- fora de uma observacao deliberada,
    DRY_RUN=false continua sendo uma escolha valida do operador."""
    if is_active() and not settings.dry_run:
        raise RuntimeError(
            f"Observacao ativa (OBSERVATION_RUN_ID={settings.observation_run_id}) mas "
            "DRY_RUN=false -- recusando iniciar. Coleta e o objetivo desta etapa, nunca "
            "acao real (CLAUDE.md regra #3). Configure DRY_RUN=true (padrao) ou desative "
            "OBSERVATION_RUN_ID se a intencao e mesmo agir de verdade fora da observacao."
        )
