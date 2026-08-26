#!/usr/bin/env python3
"""Publica no Agent Registry um `brand-agent-{brand_id}` (ver `brand_agent.py`)
para cada marca monitorada em `plane1_ingestion.prefilter.MONITORED_BRANDS`,
e semeia o `BrandContext` inicial de cada uma em `brand_context/{brand_id}`.

Mesmo padrao de `seed_registry.py`: idempotente (`publish_agent`/
`publish_brand_context` sobrescrevem o mesmo documento sem erro), seguro
para reexecutar.

## Sobre as marcas escolhidas

O pedido original deste sprint cita "Nubank, Loggi, Itau" como exemplo. O
prefiltro do projeto (`plane1_ingestion/prefilter.py::MONITORED_BRANDS`) ja
monitora tres marcas reais: nubank, loggi, ifood -- Itau NAO esta na lista
hoje. Este script semeia BrandAgents para as marcas que o pipeline JA
detecta (nubank/loggi/ifood), nao para "Itau", que exigiria antes alterar
`MONITORED_BRANDS`/`TRUSTED_DOMAINS` do prefiltro -- mudanca fora do escopo
"aditivo" deste sprint e que o CLAUDE.md pede para nao fazer sem
justificativa. Ver resumo do sprint para o time revisar essa decisao.

Uso:
    python seed_brand_agents.py
"""

from __future__ import annotations

from datetime import datetime, timezone

from brand_agent import BrandContext, agent_id_for_brand, publish_brand_context
from plane1_ingestion.prefilter import MONITORED_BRANDS, TRUSTED_DOMAINS
from registry import AgentManifest, AgentStatus, publish_agent

_NOW = datetime.now(timezone.utc)

# Metadados por marca -- os campos que um analista humano preencheria hoje
# (nome de exibicao, contato de abuso interno, tolerancia a risco). Nao
# inventados a esmo: `legitimate_domains` reusa exatamente
# `TRUSTED_DOMAINS` do prefiltro (unica fonte de verdade ja existente no
# projeto para "dominio legitimo desta marca"), filtrado por sufixo.
_BRAND_METADATA: dict[str, dict[str, object]] = {
    "nubank": {
        "display_name": "Nubank",
        "risk_tolerance": "LOW",  # instituicao financeira -- tolerancia baixa
        "confidence_escalation_threshold": 0.85,
        "abuse_contacts": ["seguranca@nubank.com.br"],
    },
    "loggi": {
        "display_name": "Loggi",
        "risk_tolerance": "MEDIUM",
        "confidence_escalation_threshold": 0.7,
        "abuse_contacts": ["seguranca@loggi.com"],
    },
    "ifood": {
        "display_name": "iFood",
        "risk_tolerance": "MEDIUM",
        "confidence_escalation_threshold": 0.7,
        "abuse_contacts": ["seguranca@ifood.com.br"],
    },
}


def _legitimate_domains_for(brand_id: str) -> list[str]:
    return sorted(d for d in TRUSTED_DOMAINS if brand_id in d)


def _build_manifest(brand_id: str) -> AgentManifest:
    from brand_agent import BrandGuidance, BrandRoutingRequest

    return AgentManifest(
        agent_id=agent_id_for_brand(brand_id),
        version="1.0.0",
        owner_team="sentinel-brand-ops",
        description=(
            f"Contexto e politica de escalonamento especificos da marca '{brand_id}': "
            "dominios legitimos, contatos de abuso, tolerancia a risco e limiar de "
            "confianca proprio. Roteado pelo orquestrador via registry.invoke_agent "
            "quando matched_brand corresponde. Ver brand_agent.py."
        ),
        input_schema=BrandRoutingRequest.model_json_schema(),
        output_schema=BrandGuidance.model_json_schema(),
        tools_allowed=["firestore.read"],
        required_permissions=["roles/datastore.user"],
        sla_seconds=2.0,
        status=AgentStatus.ACTIVE,
        created_at=_NOW,
    )


def _build_context(brand_id: str) -> BrandContext:
    meta = _BRAND_METADATA[brand_id]
    return BrandContext(
        brand_id=brand_id,
        display_name=meta["display_name"],
        legitimate_domains=_legitimate_domains_for(brand_id),
        known_typosquat_patterns=[],
        abuse_contacts=meta["abuse_contacts"],
        risk_tolerance=meta["risk_tolerance"],
        confidence_escalation_threshold=meta["confidence_escalation_threshold"],
        created_at=_NOW,
        updated_at=_NOW,
    )


def seed() -> None:
    for brand_id in MONITORED_BRANDS:
        if brand_id not in _BRAND_METADATA:
            print(f"pulado: '{brand_id}' esta em MONITORED_BRANDS mas sem metadados em _BRAND_METADATA")
            continue
        manifest = publish_agent(_build_manifest(brand_id))
        context = publish_brand_context(_build_context(brand_id))
        print(f"publicado: {manifest.doc_id} + BrandContext('{context.brand_id}')")


if __name__ == "__main__":
    seed()
