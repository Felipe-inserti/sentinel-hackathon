"""Agent Registry -- repositorio central de manifestos de agentes do Sentinel.

Requisito da trilha (Fortified Enterprise Fleet): um repositorio para
publicar, versionar e descobrir agentes aprovados. Antes deste modulo, o
orquestrador (`plane2_agents/orchestrator.py`) assumia implicitamente sua
propria "versao" e o formato de payload que aceita -- tudo hard-coded no
codigo Python. A partir de agora essa informacao vira dado publicado em
Firestore (colecao `agent_registry`, ver `config.settings.agent_registry_collection`),
e o orquestrador descobre e valida contra ela em tempo de execucao via
`invoke_agent` (ver docstring abaixo) -- adicionar um agente novo, ou uma
nova versao de um existente, passa a ser "publicar um manifesto", nao fazer
deploy de codigo.

## Modelo de dados

Um `AgentManifest` e imutavel por construcao (`agent_id` + `version` semver
formam a chave do documento, `doc_id`) -- nao existe "editar" um manifesto
publicado, so publicar uma versao nova ou trocar o `status` de uma
existente (`deprecate_agent`). `input_schema`/`output_schema` sao dicts de
JSON Schema (tipicamente gerados via `AlgumModeloPydantic.model_json_schema()`
pelo publicador, ver `seed_registry.py`) -- Pydantic valida a FORMA do
manifesto, JSON Schema valida o CONTEUDO de cada payload trocado com o
agente.

## Por que Firestore, nao um arquivo estatico versionado no repo

O registry precisa ser consultado em tempo de execucao por processos
diferentes (orquestrador, e futuramente outros agentes) sem exigir deploy
para refletir uma mudanca de status (ex: depreciar uma versao com bug em
producao sem esperar pipeline de CI/CD) -- e exatamente o caso de uso que
Firestore ja resolve no resto do projeto (cache de investigacoes, metricas
compartilhadas entre Plano 1 e Plano 2). Nao introduz nenhuma dependencia
de infraestrutura nova.

## `invoke_agent`: o ponto unico de descoberta + validacao

Este e o mecanismo que substitui o import hard-coded. Antes de executar
QUALQUER logica de agente, o chamador resolve o manifesto atual (versao
explicita ou a ultima com `status=ACTIVE`) e valida o payload recebido
contra o `input_schema` publicado. Duas formas de recusa, ambas levantando
`AgentInvocationError` com uma mensagem clara e sempre logadas ANTES de
levantar a excecao (requisito: "erro auditavel"):

  1. O manifesto resolvido nao esta `ACTIVE` (cobre pedir uma versao
     `DEPRECATED`/`DISABLED` explicitamente, ou nao haver nenhuma versao
     `ACTIVE` publicada).
  2. O payload nao valida contra o `input_schema` do manifesto.

`invoke_agent` NUNCA executa a logica do agente -- so resolve e valida.
Quem chama continua responsavel por rodar o agente e por carimbar
`agent_id`/`agent_version` (do manifesto devolvido) no resultado que
persistir, conforme regra da trilha ("todo dossie registra qual agente e
versao o produziu").
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from enum import Enum
from typing import Any

import jsonschema
from google.cloud import firestore
from pydantic import BaseModel, Field, field_validator

from config import settings

logger = logging.getLogger("registry")

# kebab-case simples: letras/digitos minusculos separados por hifen simples
# (ex: "ct-listener", "takedown-agent"). Usado como parte da chave do
# documento no Firestore (`doc_id`) -- restringir o formato evita IDs de
# documento acidentalmente ambiguos ou invalidos.
_AGENT_ID_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
# Semver estrito MAJOR.MINOR.PATCH (sem pre-release/build metadata -- nao
# ha necessidade disso neste projeto, adicionar suporte seria abstracao
# especulativa).
_SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


class AgentStatus(str, Enum):
    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"
    DISABLED = "DISABLED"


class AgentManifest(BaseModel):
    """Contrato publicado de um agente. Ver docstring do modulo."""

    agent_id: str
    version: str
    owner_team: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    tools_allowed: list[str] = Field(default_factory=list)
    required_permissions: list[str] = Field(default_factory=list)
    sla_seconds: float = Field(gt=0)
    status: AgentStatus = AgentStatus.ACTIVE
    created_at: datetime

    @field_validator("agent_id")
    @classmethod
    def _validate_agent_id(cls, value: str) -> str:
        if not _AGENT_ID_PATTERN.match(value):
            raise ValueError(
                f"agent_id {value!r} invalido -- use kebab-case (ex: 'ct-listener')"
            )
        return value

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        if not _SEMVER_PATTERN.match(value):
            raise ValueError(
                f"version {value!r} invalida -- use semver estrito (ex: '1.0.0')"
            )
        return value

    @property
    def doc_id(self) -> str:
        """Chave do documento no Firestore: `{agent_id}@{version}`."""
        return f"{self.agent_id}@{self.version}"

    @property
    def version_tuple(self) -> tuple[int, int, int]:
        major, minor, patch = self.version.split(".")
        return int(major), int(minor), int(patch)


class AgentNotFoundError(LookupError):
    """Nenhum manifesto encontrado para o `agent_id`/versao pedido."""


class AgentInvocationError(RuntimeError):
    """Invocacao recusada por `invoke_agent`: agente nao ACTIVE, ou payload
    fora do `input_schema` publicado. Sempre precedida de um log de erro
    (ver `invoke_agent`) -- a excecao em si carrega a mensagem auditavel."""


db = firestore.Client()


def _manifest_to_doc(manifest: AgentManifest) -> dict[str, Any]:
    """`created_at` fica como `datetime` nativo (Firestore grava como
    Timestamp, mesmo padrao de `orchestrator._save_investigation`);
    `status` vira string plana (Firestore/JSON Schema nao conhecem Enum
    do Python)."""
    data = manifest.model_dump(mode="python")
    data["status"] = manifest.status.value
    return data


def _doc_to_manifest(doc: firestore.DocumentSnapshot) -> AgentManifest:
    return AgentManifest.model_validate(doc.to_dict())


def publish_agent(manifest: AgentManifest) -> AgentManifest:
    """Publica `manifest` em `agent_registry/{agent_id}@{version}`.

    Idempotente por design: publicar o mesmo `agent_id`+`version` de novo
    sobrescreve o documento (permite reexecutar `seed_registry.py` com
    seguranca). Versionamento real de mudanca de contrato deve vir
    acompanhado de um `version` novo, nao de sobrescrever uma versao ja
    publicada -- isso e uma convencao de uso, nao uma trava de codigo
    (adicionar essa trava seria uma garantia que o projeto nao pediu)."""
    doc_ref = db.collection(settings.agent_registry_collection).document(manifest.doc_id)
    already_existed = doc_ref.get().exists
    doc_ref.set(_manifest_to_doc(manifest))
    logger.info(
        "Agente %s (status=%s) -- %s",
        manifest.doc_id,
        manifest.status.value,
        "republicado" if already_existed else "publicado",
    )
    return manifest


def get_agent(agent_id: str, version: str | None = None) -> AgentManifest:
    """Busca um manifesto.

    Com `version`: devolve o manifesto exato, de QUALQUER status (uso de
    auditoria/inspecao -- ex: um dashboard mostrando historico completo).

    Sem `version`: devolve a versao mais alta (semver) com `status=ACTIVE`
    -- esta e a semantica de "descoberta" usada por `invoke_agent`.

    Levanta `AgentNotFoundError` se nada correspondente existir."""
    if version is not None:
        doc = db.collection(settings.agent_registry_collection).document(f"{agent_id}@{version}").get()
        if not doc.exists:
            raise AgentNotFoundError(f"Agente '{agent_id}' versao '{version}' nao encontrado no registry")
        return _doc_to_manifest(doc)

    candidates = [
        _doc_to_manifest(doc)
        for doc in (
            db.collection(settings.agent_registry_collection)
            .where("agent_id", "==", agent_id)
            .where("status", "==", AgentStatus.ACTIVE.value)
            .stream()
        )
    ]
    if not candidates:
        raise AgentNotFoundError(f"Nenhuma versao ACTIVE do agente '{agent_id}' encontrada no registry")
    return max(candidates, key=lambda m: m.version_tuple)


def list_agents(
    *,
    agent_id: str | None = None,
    status: AgentStatus | None = None,
    owner_team: str | None = None,
) -> list[AgentManifest]:
    """Lista manifestos, filtravel por qualquer combinacao de `agent_id`,
    `status` e `owner_team`. Sem filtros, devolve o registry inteiro."""
    query: firestore.Query = db.collection(settings.agent_registry_collection)
    if agent_id is not None:
        query = query.where("agent_id", "==", agent_id)
    if status is not None:
        query = query.where("status", "==", status.value)
    if owner_team is not None:
        query = query.where("owner_team", "==", owner_team)
    return [_doc_to_manifest(doc) for doc in query.stream()]


def deprecate_agent(agent_id: str, version: str) -> AgentManifest:
    """Marca `agent_id@version` como `DEPRECATED`. Nao apaga o manifesto
    (historico/auditoria continuam consultaveis via `get_agent` com
    versao explicita). Levanta `AgentNotFoundError` se a versao nao existir."""
    doc_ref = db.collection(settings.agent_registry_collection).document(f"{agent_id}@{version}")
    if not doc_ref.get().exists:
        raise AgentNotFoundError(f"Agente '{agent_id}' versao '{version}' nao encontrado no registry")
    doc_ref.update({"status": AgentStatus.DEPRECATED.value})
    logger.warning("Agente depreciado: %s@%s", agent_id, version)
    return get_agent(agent_id, version)


def invoke_agent(
    agent_id: str, payload: dict[str, Any], *, version: str | None = None
) -> AgentManifest:
    """Resolve e valida uma invocacao de agente -- ver docstring do modulo.
    Devolve o `AgentManifest` resolvido (para o chamador carimbar
    `agent_id`/`agent_version` no que ele persistir); nunca executa a
    logica do agente."""
    try:
        manifest = get_agent(agent_id, version)
    except AgentNotFoundError:
        logger.error(
            "Invocacao rejeitada: agente '%s' (versao=%s) nao existe no registry",
            agent_id,
            version or "ultima ACTIVE",
        )
        raise

    if manifest.status != AgentStatus.ACTIVE:
        logger.error(
            "Invocacao rejeitada: agente %s esta %s -- apenas ACTIVE pode ser invocado",
            manifest.doc_id,
            manifest.status.value,
        )
        raise AgentInvocationError(
            f"Agente '{manifest.doc_id}' esta com status {manifest.status.value} -- "
            "apenas agentes ACTIVE podem ser invocados"
        )

    try:
        jsonschema.validate(instance=payload, schema=manifest.input_schema)
    except jsonschema.ValidationError as exc:
        logger.error(
            "Invocacao rejeitada: payload fora do input_schema de %s: %s (payload=%s)",
            manifest.doc_id,
            exc.message,
            payload,
        )
        raise AgentInvocationError(
            f"Payload invalido para '{manifest.doc_id}': {exc.message}"
        ) from exc

    return manifest
