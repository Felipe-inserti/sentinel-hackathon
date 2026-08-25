"""Execucao de takedown -- stub minimo e seguro.

Este modulo NAO implementa um agente de takedown de verdade (resolucao de
destinatario via RDAP, allowlist, aprovacao humana registrada -- tudo isso
descrito como regra obrigatoria no CLAUDE.md -- ainda nao existe no
projeto). Ele existe so para hospedar o span `takedown.execute` exigido
pelo vocabulario de instrumentacao da trilha (ver telemetry.py), de forma
honesta: em DRY_RUN (o padrao), registra a intencao e nao contata ninguem;
fora de DRY_RUN, recusa explicitamente em vez de fingir que enviou algo.

Nao e chamado automaticamente por `orchestrator.py` -- disparar um
takedown sem um mecanismo real de aprovacao humana violaria a regra do
CLAUDE.md ("Nenhum takedown sem aprovacao humana registrada"). Fica aqui
como ponto de entrada para quando esse mecanismo existir.
"""

from __future__ import annotations

import logging

import telemetry
from config import settings

logger = logging.getLogger("takedown")


class TakedownNotImplementedError(RuntimeError):
    """Levantado quando alguem tenta executar um takedown de verdade (fora
    de DRY_RUN) sem que o mecanismo de aprovacao humana/resolucao de
    destinatario exista ainda."""


async def execute_takedown(domain: str, decision_rationale: str) -> dict[str, object]:
    """Abre o span `takedown.execute`. Em DRY_RUN (padrao), so loga a
    intencao -- nenhuma notificacao real e enviada. Fora de DRY_RUN,
    recusa explicitamente (ver docstring do modulo)."""
    tracer = telemetry.get_tracer()
    with tracer.start_as_current_span("takedown.execute") as span:
        span.set_attribute("takedown.domain", domain)
        span.set_attribute("takedown.dry_run", settings.dry_run)

        if settings.dry_run:
            logger.info(
                "DRY_RUN: takedown NAO enviado para %s (motivo registrado: %s)",
                domain,
                decision_rationale,
            )
            span.set_attribute("takedown.sent", False)
            return {"domain": domain, "sent": False, "dry_run": True}

        span.set_attribute("takedown.sent", False)
        span.set_attribute("takedown.error", "not_implemented")
        raise TakedownNotImplementedError(
            "Takedown real exige resolucao de destinatario via RDAP, allowlist e "
            "aprovacao humana registrada (approved_by/approved_at/decision_rationale "
            "no Firestore) -- nenhum desses mecanismos existe ainda. Ver CLAUDE.md."
        )
