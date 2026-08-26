#!/usr/bin/env python3
"""Varre `investigations` por decisoes humanas terminais (status
`REJECTED`/`TAKEDOWN_APPROVED`) de marcas com BrandAgent publicado, e as
espelha em `brand_memory` via `brand_memory.record_rejection`/
`record_approval` (ver `brand_memory.py`).

## Por que pull (varredura), nao push (Pub/Sub) neste sprint

`dashboard/src/app/(app)/review/actions.ts::rejectInvestigation` hoje SO
grava Firestore -- o proprio comentario no codigo ja documenta isso como
"alimenta o feedback loop de um sprint futuro, ainda nao implementado"
(este sprint, Parte B). Fechar esse loop via um evento novo publicado pelo
dashboard mudaria `dashboard/` ja deployado, fora do que este sprint pode
alterar sem aprovacao explicita (regra da sessao: "nao altere... o
dashboard ja deployado sem me perguntar antes"). Esta varredura cobre o
requisito ("toda rejeicao humana vira entrada de memoria") de forma
aditiva: rode periodicamente (cron manual, ou antes de uma demo) para
manter `brand_memory` sincronizada com as decisoes tomadas no dashboard.

Idempotente: `brand_memory._memory_doc_id` e deterministico
(marca+dominio+tipo+timestamp da decisao humana), entao rodar este script
repetidas vezes NUNCA duplica uma entrada ja sincronizada -- so novas
decisoes desde a ultima execucao geram entradas novas.

Uso:
    python sync_brand_memory.py                # todas as marcas monitoradas
    python sync_brand_memory.py --brand nubank  # so uma marca
    python sync_brand_memory.py --limit 200
"""

from __future__ import annotations

import argparse
import logging

import brand_agent
import brand_memory
from plane1_ingestion.prefilter import MONITORED_BRANDS

logger = logging.getLogger("sync_brand_memory")

_DECIDED_STATUSES = ["REJECTED", "TAKEDOWN_APPROVED"]


def sync_brand(brand_id: str, *, limit: int = 500) -> tuple[int, int]:
    """Sincroniza uma unica marca. Devolve
    `(rejeicoes_sincronizadas, aprovacoes_sincronizadas)`. So le
    `investigations` atraves de `BrandScopedInvestigations` (isolamento
    por marca, ver `brand_agent.py`) -- nunca uma query solta neste
    script."""
    scope = brand_agent.BrandScopedInvestigations(brand_id)
    decided = scope.list_by_status(_DECIDED_STATUSES, limit=limit)

    rejected_count = 0
    approved_count = 0
    for doc in decided:
        domain = doc.get("domain")
        if not domain:
            logger.warning("Documento decidido sem campo 'domain' para marca '%s', pulando", brand_id)
            continue
        status = doc.get("status")
        try:
            if status == "REJECTED":
                brand_memory.record_rejection(brand_id=brand_id, domain=domain, investigation=doc)
                rejected_count += 1
            elif status == "TAKEDOWN_APPROVED":
                brand_memory.record_approval(brand_id=brand_id, domain=domain, investigation=doc)
                approved_count += 1
        except Exception:
            logger.exception("Falha ao sincronizar memoria para '%s' (marca '%s')", domain, brand_id)

    return rejected_count, approved_count


def sync_all(*, limit: int = 500) -> dict[str, tuple[int, int]]:
    report: dict[str, tuple[int, int]] = {}
    for brand_id in MONITORED_BRANDS:
        report[brand_id] = sync_brand(brand_id, limit=limit)
    return report


def _print_report(brand_id: str, rejected: int, approved: int) -> None:
    print(f"{brand_id}: {rejected} rejeicao(oes) + {approved} aprovacao(oes) sincronizadas em brand_memory")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--brand", help="Sincronizar so esta marca (default: todas em MONITORED_BRANDS)")
    parser.add_argument("--limit", type=int, default=500, help="Maximo de dossies decididos por marca (default 500)")
    args = parser.parse_args()

    if args.brand:
        rejected, approved = sync_brand(args.brand, limit=args.limit)
        _print_report(args.brand, rejected, approved)
    else:
        for brand_id, (rejected, approved) in sync_all(limit=args.limit).items():
            _print_report(brand_id, rejected, approved)
