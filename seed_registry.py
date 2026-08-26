#!/usr/bin/env python3
"""Publica no Agent Registry (ver `registry.py`) os manifestos dos quatro
agentes que compoem a arquitetura do Sentinel: `ct-listener`, `orchestrator`
e `evidence-collector` (codigo real, rodando hoje) e `takedown-agent`
(contrato reservado -- `takedown.py` ja existe como stub honesto, ver sua
docstring). `evidence-collector` tem duas versoes publicadas: `1.0.0`
(DEPRECATED, contrato reservado que se mostrou incompleto -- ver comentario
acima de `EvidenceCollectionOutput`) e `2.0.0` (ACTIVE, implementada em
`evidence_agent.py`).

Os `input_schema`/`output_schema` abaixo sao gerados via
`.model_json_schema()` a partir de modelos Pydantic que espelham os
payloads REAIS ja trocados pelo pipeline (mensagens Pub/Sub de
`plane1_ingestion/ct_listener.py` e `plane2_agents/orchestrator.py`) -- nao
sao inventados. Rodar este script e seguro/idempotente: `publish_agent`
sobrescreve o mesmo `agent_id@version` sem erro (ver `registry.py`).

Uso:
    python seed_registry.py
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from evidence_agent import EvidenceBundle
from registry import AgentManifest, AgentStatus, publish_agent

# --- Contratos (fonte dos JSON Schemas publicados) -------------------------


class SuspiciousDomainSignal(BaseModel):
    """Payload publicado em `suspicious-domain-detected` por
    `ct_listener.py::_publish_suspicious_domain` -- e tambem o payload que
    `orchestrator.py` consome via `sub-orchestrator`. So `domain` e
    obrigatorio; os demais campos ja existem na mensagem real mas o
    orquestrador so le `domain`/`matched_brand` hoje (ver
    `_handle_pubsub_message`) -- deixa-los declarados (e opcionais) no
    schema evita que o payload real e completo seja rejeitado na validacao
    de `invoke_agent` por causa de campos extras."""

    domain: str
    matched_brand: str | None = None
    prefilter_score: float | None = None
    priority: str | None = None
    source: str | None = None
    detected_at: float | None = None


class CertstreamEvent(BaseModel):
    """Subconjunto dos campos do evento certstream que `ct_listener.py`
    realmente le (ver `_extract_domains`/`_extract_certificate_age_seconds`).
    Documentado por completude do catalogo -- `ct-listener` consome um
    stream publico externo (websocket do certstream.calidog.io), entao este
    schema e informativo (o que o agente aceita como entrada de fato), nao
    uma barreira de validacao aplicada em runtime como a de `orchestrator`
    -- nao ha um ponto de invocacao controlado (Pub/Sub) para isso do lado
    da ingestao."""

    message_type: str
    data: dict[str, Any] = Field(default_factory=dict)


class InvestigationCompletedMessage(BaseModel):
    """Payload publicado em `investigation-completed` por
    `orchestrator.py::_publish_completed` -- output_schema de
    `orchestrator` e input natural de um futuro consumidor como
    `evidence-collector`."""

    domain: str
    classification: Literal["MALICIOUS", "SAFE"]
    confidence: float
    cache_hit: bool


# O output_schema de evidence-collector@1.0.0 (este `EvidenceCollectionOutput`,
# mantido so por historico/auditoria -- NAO usado em nenhum manifesto abaixo)
# era um contrato reservado, publicado antes de existir qualquer codigo do
# agente: so 4 campos (domain, evidence_gcs_uri, captured_at, pii_redacted),
# sem lugar nenhum para DNS/ASN/TLS/RDAP/hashes por artefato/hash raiz -- ou
# seja, nao comportava chain of custody nenhuma. Ao implementar o agente
# (Sprint 4, ver evidence_agent.py), essa divergencia foi reportada ANTES de
# escrever qualquer logica (conforme instrucao explicita: nunca implementar
# algo divergente do contrato, nem alterar o manifesto em silencio) --
# aprovada a correcao, o `EvidenceBundle` real definido em evidence_agent.py
# passou a ser a fonte do output_schema, publicado como `evidence-collector@2.0.0`
# (ver MANIFESTS abaixo); a v1.0.0 fica marcada DEPRECATED, nao apagada.
class EvidenceCollectionOutput(BaseModel):
    """Historico apenas -- ver comentario acima. Corresponde ao
    output_schema de evidence-collector@1.0.0 (DEPRECATED)."""

    domain: str
    evidence_gcs_uri: str
    captured_at: datetime
    pii_redacted: bool


class TakedownApprovalMessage(BaseModel):
    """Payload publicado em `takedown-approved` -- UNICO gatilho que
    `takedown-sa` tem permissao de consumir (ver `infra/`). `channel` e um
    enum fechado (regra de seguranca #2 do CLAUDE.md: o LLM escolhe o
    canal, nunca o endereco real do destinatario -- isso e resolvido por
    codigo deterministico via RDAP)."""

    domain: str
    channel: Literal["registrar_abuse", "hosting_abuse", "brand_protection_vendor"]
    approved_by: str
    approved_at: datetime
    decision_rationale: str


class TakedownExecutionOutput(BaseModel):
    """Espelha o retorno real de `takedown.execute_takedown` (ver
    `takedown.py`)."""

    domain: str
    sent: bool
    dry_run: bool


# --- Manifestos --------------------------------------------------------

_NOW = datetime.now(timezone.utc)

MANIFESTS: tuple[AgentManifest, ...] = (
    AgentManifest(
        agent_id="ct-listener",
        version="1.0.0",
        owner_team="sentinel-ingestion",
        description=(
            "Consome o stream publico de Certificate Transparency, aplica o "
            "prefiltro determinístico (zero LLM) e a triagem em lote via "
            "Gemma 3 270M, e publica sobreviventes em suspicious-domain-detected."
        ),
        input_schema=CertstreamEvent.model_json_schema(),
        output_schema=SuspiciousDomainSignal.model_json_schema(),
        tools_allowed=["certstream.websocket", "pubsub.publish"],
        required_permissions=["roles/pubsub.publisher"],
        sla_seconds=5.0,
        status=AgentStatus.ACTIVE,
        created_at=_NOW,
    ),
    AgentManifest(
        agent_id="orchestrator",
        version="1.0.0",
        owner_team="sentinel-investigation",
        description=(
            "Cache-first: consulta Firestore antes de gastar qualquer token; "
            "em cache miss, raspa a pagina (deterministico) e classifica com "
            "Gemini via saida estruturada, publicando em investigation-completed."
        ),
        input_schema=SuspiciousDomainSignal.model_json_schema(),
        output_schema=InvestigationCompletedMessage.model_json_schema(),
        tools_allowed=[
            "pubsub.subscribe",
            "http.scrape",
            "vertex_ai.generate_content",
            "firestore.read",
            "firestore.write",
            "pubsub.publish",
        ],
        required_permissions=[
            "roles/pubsub.subscriber",
            "roles/pubsub.publisher",
            "roles/datastore.user",
            "roles/aiplatform.user",
        ],
        sla_seconds=20.0,
        status=AgentStatus.ACTIVE,
        created_at=_NOW,
    ),
    AgentManifest(
        agent_id="evidence-collector",
        version="1.0.0",
        owner_team="sentinel-evidence",
        description=(
            "DEPRECATED -- superado por evidence-collector@2.0.0. Contrato "
            "reservado original (sem codigo, so 4 campos de output) nao "
            "comportava chain of custody; ver EvidenceCollectionOutput acima "
            "e evidence_agent.py para o historico da correcao."
        ),
        input_schema=InvestigationCompletedMessage.model_json_schema(),
        output_schema=EvidenceCollectionOutput.model_json_schema(),
        tools_allowed=["cloud_storage.write", "firestore.read", "firestore.write"],
        required_permissions=["roles/storage.objectAdmin", "roles/datastore.user"],
        sla_seconds=15.0,
        status=AgentStatus.DEPRECATED,
        created_at=_NOW,
    ),
    AgentManifest(
        agent_id="evidence-collector",
        version="2.0.0",
        owner_team="sentinel-evidence",
        description=(
            "Coleta deterministica (zero LLM) de evidencia verificavel para "
            "dominios MALICIOUS: screenshot full-page (Playwright, dominio "
            "travado), HTML/headers/redirects, DNS, hospedagem/ASN, cadeia "
            "de certificado TLS, RDAP (com domain_age_hours em destaque), "
            "fingerprint de infraestrutura e chain of custody (SHA-256 por "
            "artefato + hash raiz do manifesto). Ver evidence_agent.py."
        ),
        input_schema=InvestigationCompletedMessage.model_json_schema(),
        output_schema=EvidenceBundle.model_json_schema(),
        tools_allowed=[
            "pubsub.subscribe",
            "playwright.headless_browser",
            "http.scrape",
            "dns.resolve",
            "cloud_storage.write",
            "firestore.read",
            "firestore.write",
        ],
        required_permissions=[
            "roles/pubsub.subscriber",
            "roles/storage.objectAdmin",
            "roles/datastore.user",
        ],
        sla_seconds=30.0,
        status=AgentStatus.ACTIVE,
        created_at=_NOW,
    ),
    AgentManifest(
        agent_id="takedown-agent",
        version="1.0.0",
        owner_team="sentinel-response",
        description=(
            "Executa notificacao de takedown (DRY_RUN=true por padrao, ver "
            "takedown.py). So pode ser acionado por uma aprovacao ja "
            "publicada em takedown-approved -- por permissao IAM (takedown-sa "
            "so tem roles/pubsub.subscriber nesse topico, ver infra/), nao "
            "so por logica de codigo."
        ),
        input_schema=TakedownApprovalMessage.model_json_schema(),
        output_schema=TakedownExecutionOutput.model_json_schema(),
        tools_allowed=["pubsub.subscribe"],
        required_permissions=["roles/pubsub.subscriber"],
        sla_seconds=5.0,
        status=AgentStatus.ACTIVE,
        created_at=_NOW,
    ),
)


def seed() -> None:
    for manifest in MANIFESTS:
        publish_agent(manifest)
        print(f"publicado: {manifest.doc_id} ({manifest.owner_team})")


if __name__ == "__main__":
    seed()
