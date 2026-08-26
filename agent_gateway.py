"""Agent Gateway -- ponto UNICO de entrada para invocar qualquer agente do
Sentinel (requisito "Agent Gateway" da trilha Fortified Enterprise Fleet).

Contexto: os quatro agentes existentes (`ct-listener`, `orchestrator`,
`evidence-collector`, `takedown-agent`) sao workers Pub/Sub de longa
duracao, nao servicos HTTP -- nada neste projeto, ate agora, expunha um
jeito controlado e uniforme de disparar uma invocacao "de fora" (dashboard,
outro sistema, um jurado testando a trilha). Este modulo e esse ponto
unico: todo POST /invoke/{agent_id} passa pelo MESMO pipeline, NESTA
ORDEM, antes de qualquer efeito colateral:

  1. **Autenticacao (Agent Identity)** -- `Authorization: Bearer <ID token
     do Google>`. O token precisa ser assinado de verdade pelo Google
     (verificado via `google.oauth2.id_token.verify_oauth2_token`, nunca
     decodificado sem checar assinatura) e sua claim `email` vira a
     IDENTIDADE do chamador -- o mesmo conceito de identidade que
     `infra/main.tf` ja materializa como uma Service Account por agente
     (`ct-listener-sa`, `orchestrator-sa`, `evidence-sa`, `takedown-sa`,
     `dashboard-sa`). Quem chama o gateway se autentica como uma dessas
     contas (ou outra autorizada), nunca com um segredo compartilhado.
  2. **Resolucao no registry** -- `registry.get_agent(agent_id, version)`.
     Sem `version` explicita, resolve a versao mais alta com
     `status=ACTIVE` (mesma semantica de `registry.py`). Pedir uma versao
     `DEPRECATED`/`DISABLED` explicitamente, ou um `agent_id` inexistente,
     e rejeitado aqui.
  3. **Validacao de schema** -- `jsonschema.validate(payload,
     manifest.input_schema)`. Separada da resolucao (ainda que
     `registry.invoke_agent` faca as duas em sequencia) para que o erro
     estruturado aponte exatamente qual das duas etapas falhou.
  4. **Rate limit** -- contador transacional no Firestore por
     `(identidade chamadora, agent_id, minuto UTC)`, mesmo mecanismo de
     `takedown_agent.py::_check_and_increment_rate_limit` (transacao
     `firestore.transactional`), so que por minuto em vez de dia --
     protege o caminho sincrono do gateway contra abuso de chamada, nao
     contra volume diario de notificacao externa (isso continua sendo
     `takedown_rate_limit_collection`, papel do proprio `takedown_agent.py`).
  5. **Politica de autorizacao** -- `AUTHORIZATION_POLICY` abaixo: quais
     identidades podem invocar qual `agent_id`. `takedown-agent` e
     `frozenset()` -- NINGUEM, nem `dashboard-sa`, pode aciona-lo por
     aqui: decisao arquitetural deliberada (nao uma lacuna), ver nota de
     seguranca extensa em `AUTHORIZATION_POLICY` e `infra/README.md`. O
     gateway NUNCA ganha `roles/pubsub.publisher` em `takedown-approved`
     -- `dashboard-sa` continua sendo o UNICO publisher desse topico, a
     garantia topologica original permanece intacta, nao "mais uma
     camada dela".
  6. **Roteamento** -- publica o payload validado no topico Pub/Sub que
     aquele agente consome (`AGENT_ROUTING_TOPIC` abaixo), usando a
     identidade do PROCESSO do gateway (a SA do Cloud Run que o roda, ver
     Parte B), nunca a do chamador -- o chamador nunca ganha uma
     credencial de publish direta. `ct-listener` (websocket publico de
     terceiros, sem fila controlada, ver `seed_registry.py::
     CertstreamEvent`) e `takedown-agent` (bloqueado na etapa 5) ficam de
     fora de `AGENT_ROUTING_TOPIC` -- invoca-los via gateway e recusado
     com erro estruturado proprio, nunca uma falha silenciosa.
  7. **Log de auditoria** -- um documento NOVO por chamada (nunca
     atualizado) em `agent_gateway_audit_log`, para toda chamada --
     sucesso OU rejeicao em QUALQUER etapa acima. Mesma disciplina de
     `takedown_agent.py::_write_audit_record`: uma rejeicao tambem e uma
     decisao de seguranca, e precisa ficar auditavel.

`GET /agents` lista o registry inteiro (todos os status, sem filtro --
uso de inspecao, mesma semantica de `registry.list_agents()` sem
argumentos), tambem atras de autenticacao.

Runtime: FastAPI/uvicorn -- o UNICO servico HTTP sincrono deste projeto
alem do dashboard Next.js (os quatro agentes sao workers Pub/Sub, ver
CLAUDE.md). `async`/`await` para toda I/O; Firestore/Pub/Sub continuam
sendo clientes SINCRONOS do SDK oficial, chamados via `asyncio.to_thread`
-- mesmo padrao ja usado em `orchestrator.py`/`takedown_agent.py`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Literal

import jsonschema
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from google.cloud import firestore, pubsub_v1
from pydantic import BaseModel, Field

import registry
from config import settings

logger = logging.getLogger("agent_gateway")

GatewayStage = Literal[
    "authentication",
    "resolution",
    "schema_validation",
    "rate_limit",
    "authorization",
    "routing",
    "internal",
]

# Status HTTP por etapa de rejeicao -- consistente em toda resposta de erro
# (nunca um 500 generico escondendo qual camada de seguranca recusou).
_STAGE_HTTP_STATUS: dict[GatewayStage, int] = {
    "authentication": 401,
    "resolution": 404,
    "schema_validation": 422,
    "rate_limit": 429,
    "authorization": 403,
    "routing": 400,
    "internal": 500,
}


# ---------------------------------------------------------------------------
# Politica de autorizacao e tabela de roteamento
# ---------------------------------------------------------------------------

# `None` = qualquer identidade autenticada (passou na etapa 1) pode invocar.
# `frozenset(...)` = so essas identidades. Um `agent_id` AUSENTE deste dict
# (`.get(agent_id, frozenset())`, ver `_authorize`) tambem vale "ninguem" --
# nega por padrao qualquer agente sem politica explicita, em vez de
# permitir por omissao.
#
# `ct-listener` fica deliberadamente com `None` (qualquer identidade
# autenticada pode PEDIR) em vez de `frozenset()`: ele nao tem policy de
# autorizacao mais restrita que os outros, so nao esta em
# `AGENT_ROUTING_TOPIC` -- a rejeicao correta para ele acontece na etapa 6
# (roteamento, erro "not_routable"), nao na etapa 5 (autorizacao), porque o
# MOTIVO de nao ser invocavel e arquitetural (websocket publico, sem fila
# controlada), nao uma restricao de identidade.
#
# `takedown-agent` e `frozenset()` -- NINGUEM, nem `dashboard-sa`, pode
# invoca-lo via gateway. Decisao arquitetural deliberada, revisada e
# confirmada explicitamente (nao uma lacuna a fechar depois): a garantia
# "nenhum takedown sem aprovacao humana registrada" (regra #4 do
# CLAUDE.md) e, hoje, TOPOLOGICA -- `dashboard-sa` e a UNICA identidade
# com `roles/pubsub.publisher` no topico `takedown-approved` (ver
# infra/README.md, secao "Por que takedown-sa e a peca central"). Rotear
# uma invocacao de `takedown-agent` pelo gateway exigiria dar esse MESMO
# papel tambem a SA do gateway -- um SEGUNDO publisher no topico. Foi
# cogitado (ver historico do PR) e REJEITADO: ainda que
# `takedown_agent.py::_load_verified_approval` reconfirme a aprovacao no
# Firestore antes de agir (defesa em profundidade real), a garantia mais
# forte e mais facil de auditar -- "um unico caminho de publish, o fluxo
# humano do dashboard" -- vale mais que a conveniencia de ter um segundo
# caminho sincrono para o mesmo efeito. O gateway continua UTIL para
# `takedown-agent` (aparece em GET /agents, para inspecao), so nao roteia
# nada para ele -- `_authorize` devolve uma mensagem dedicada explicando
# o motivo (ver `_TAKEDOWN_AGENT_ID` abaixo), nao o "nao autorizado"
# generico. Nenhuma SA do gateway ganha `roles/pubsub.publisher` em
# `takedown-approved` na Parte B -- ver infra/README.md.
AUTHORIZATION_POLICY: dict[str, frozenset[str] | None] = {
    "ct-listener": None,
    "orchestrator": None,
    "evidence-collector": None,
    "takedown-agent": frozenset(),
}

# `agent_id` cuja rejeicao de autorizacao merece uma mensagem PROPRIA (ver
# `_authorize`) em vez do "nao autorizado" generico -- hoje, so
# `takedown-agent` (ver comentario de `AUTHORIZATION_POLICY` acima).
_TAKEDOWN_AGENT_ID = "takedown-agent"

# Para qual topico Pub/Sub o payload validado e roteado, por `agent_id`.
# Ausente = agente nao invocavel via gateway (ver docstring do modulo,
# etapa 6). `ct-listener` (consome um websocket publico, sem ponto de
# entrada controlado) e `takedown-agent` (bloqueado na etapa 5, ver
# `AUTHORIZATION_POLICY` -- nunca chegaria aqui de qualquer forma, mas
# fica de fora tambem por clareza: nao existe topico "certo" pra rotear
# uma invocacao que nunca deveria acontecer) ficam de fora de proposito.
AGENT_ROUTING_TOPIC: dict[str, str] = {
    "orchestrator": settings.suspicious_topic_id,
    "evidence-collector": settings.completed_topic_id,
}


# ---------------------------------------------------------------------------
# Erro estruturado e auditavel
# ---------------------------------------------------------------------------


class GatewayErrorBody(BaseModel):
    """Corpo de toda resposta de erro do gateway -- nunca um 500/HTML
    generico. `stage` aponta exatamente qual etapa do pipeline (ver
    docstring do modulo) recusou a chamada."""

    stage: GatewayStage
    error: str
    detail: str
    agent_id: str
    caller_identity: str | None = None
    trace_id: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class GatewayRejection(Exception):
    """Levantada em qualquer etapa do pipeline que recusa a chamada. Quem
    levanta ja logou (auditoria em Firestore) ANTES de levantar --
    mesma disciplina de `registry.invoke_agent`/`AgentInvocationError`."""

    def __init__(self, stage: GatewayStage, error: str, detail: str) -> None:
        super().__init__(detail)
        self.stage = stage
        self.error = error
        self.detail = detail


class InvocationAccepted(BaseModel):
    """Corpo de sucesso de POST /invoke/{agent_id}."""

    agent_id: str
    agent_version: str
    caller_identity: str
    routed_to_topic: str
    message_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AgentSummary(BaseModel):
    """Item de GET /agents -- espelha o `AgentManifest` publicado, mais
    `routable` (se este gateway sabe rotear uma invocacao dele)."""

    agent_id: str
    version: str
    status: str
    owner_team: str
    description: str
    sla_seconds: float
    required_permissions: list[str]
    routable: bool
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]


# ---------------------------------------------------------------------------
# Verificacao de ID token do Google (Agent Identity)
# ---------------------------------------------------------------------------


def verify_google_id_token(token: str) -> dict[str, Any]:
    """Verifica um ID token do Google -- assinatura, emissor e expiracao
    checados pela biblioteca oficial (`google-auth`), NUNCA decodificado
    sem verificar. `settings.agent_gateway_audience` (a URL publica deste
    servico apos o deploy) e checada quando configurada; `None` (default
    de desenvolvimento local) pula so a checagem de audience -- avisado
    alto no startup (ver `create_app`), nunca silencioso. Levanta
    `ValueError` em qualquer falha; devolve os claims decodificados, dos
    quais o chamador usa `claims["email"]` como identidade."""
    from google.auth.transport import requests as google_auth_requests
    from google.oauth2 import id_token as google_id_token

    claims = google_id_token.verify_oauth2_token(
        token, google_auth_requests.Request(), audience=settings.agent_gateway_audience
    )
    if "email" not in claims:
        raise ValueError("ID token valido, mas sem claim 'email' -- nao da pra derivar identidade")
    return claims


def _extract_bearer_token(authorization_header: str | None) -> str:
    if not authorization_header or not authorization_header.startswith("Bearer "):
        raise ValueError("cabecalho Authorization ausente ou sem o prefixo 'Bearer '")
    token = authorization_header.removeprefix("Bearer ").strip()
    if not token:
        raise ValueError("token vazio apos 'Bearer '")
    return token


# ---------------------------------------------------------------------------
# AgentGateway -- pipeline principal, testavel sem FastAPI/HTTP real
# ---------------------------------------------------------------------------


class AgentGateway:
    """Implementa o pipeline de 7 etapas. Todas as dependencias externas
    (verificador de token, cliente Firestore, publisher Pub/Sub, relogio)
    sao injetaveis -- os testes trocam por fakes, nenhuma chamada de rede
    real roda em `pytest` (mesmo principio de `tests/test_registry.py`:
    Firestore sempre mockado)."""

    def __init__(
        self,
        *,
        verify_token: Callable[[str], dict[str, Any]] = verify_google_id_token,
        db: firestore.Client | None = None,
        publisher: pubsub_v1.PublisherClient | None = None,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        authorization_policy: dict[str, frozenset[str] | None] | None = None,
        routing_table: dict[str, str] | None = None,
    ) -> None:
        self._verify_token = verify_token
        self._db = db if db is not None else firestore.Client()
        self._publisher = publisher if publisher is not None else pubsub_v1.PublisherClient()
        self._now_fn = now_fn
        self._authorization_policy = (
            authorization_policy if authorization_policy is not None else AUTHORIZATION_POLICY
        )
        self._routing_table = routing_table if routing_table is not None else AGENT_ROUTING_TOPIC

    # -- etapa 1 -----------------------------------------------------------

    def _authenticate(self, authorization_header: str | None, agent_id: str) -> str:
        try:
            token = _extract_bearer_token(authorization_header)
            claims = self._verify_token(token)
            return claims["email"]
        except Exception as exc:  # noqa: BLE001 -- qualquer falha de token e igualmente "nao autenticado"
            self._audit(
                agent_id=agent_id,
                caller_identity=None,
                stage="authentication",
                outcome="REJECTED",
                error=str(exc),
                agent_version=None,
            )
            raise GatewayRejection(
                "authentication", "invalid_or_missing_credentials", str(exc)
            ) from exc

    # -- etapa 2 -------------------------------------------------------------

    def _resolve(
        self, agent_id: str, version: str | None, caller_identity: str
    ) -> registry.AgentManifest:
        try:
            manifest = registry.get_agent(agent_id, version)
        except registry.AgentNotFoundError as exc:
            self._audit(
                agent_id=agent_id,
                caller_identity=caller_identity,
                stage="resolution",
                outcome="REJECTED",
                error=str(exc),
                agent_version=version,
            )
            raise GatewayRejection("resolution", "agent_not_found", str(exc)) from exc

        if manifest.status != registry.AgentStatus.ACTIVE:
            detail = (
                f"Agente '{manifest.doc_id}' esta {manifest.status.value} -- "
                "apenas agentes ACTIVE podem ser invocados via gateway"
            )
            self._audit(
                agent_id=agent_id,
                caller_identity=caller_identity,
                stage="resolution",
                outcome="REJECTED",
                error=detail,
                agent_version=manifest.version,
            )
            raise GatewayRejection("resolution", "agent_not_active", detail)
        return manifest

    # -- etapa 3 -------------------------------------------------------------

    def _parse_payload(
        self, raw_body: bytes, manifest: registry.AgentManifest, caller_identity: str
    ) -> dict[str, Any]:
        """Desserializa o corpo cru da requisicao. Roda DEPOIS da
        autenticacao e da resolucao (etapas 1-2), mesmo com corpo
        malformado -- so entao um JSON invalido vira uma rejeicao de
        `schema_validation` (nao da pra validar contra o `input_schema`
        algo que nem parseia)."""
        try:
            return json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError as exc:
            self._audit(
                agent_id=manifest.agent_id,
                caller_identity=caller_identity,
                stage="schema_validation",
                outcome="REJECTED",
                error=str(exc),
                agent_version=manifest.version,
            )
            raise GatewayRejection("schema_validation", "malformed_json_body", str(exc)) from exc

    def _validate_schema(
        self, payload: dict[str, Any], manifest: registry.AgentManifest, caller_identity: str
    ) -> None:
        try:
            jsonschema.validate(instance=payload, schema=manifest.input_schema)
        except jsonschema.ValidationError as exc:
            self._audit(
                agent_id=manifest.agent_id,
                caller_identity=caller_identity,
                stage="schema_validation",
                outcome="REJECTED",
                error=exc.message,
                agent_version=manifest.version,
            )
            raise GatewayRejection("schema_validation", "payload_invalid", exc.message) from exc

    # -- etapa 4 -------------------------------------------------------------

    def _check_rate_limit(
        self, agent_id: str, caller_identity: str, manifest_version: str
    ) -> None:
        window = self._now_fn().strftime("%Y-%m-%dT%H:%M")
        doc_id = f"{caller_identity}__{agent_id}__{window}"
        doc_ref = self._db.collection(settings.agent_gateway_rate_limit_collection).document(doc_id)

        @firestore.transactional
        def _run(transaction: firestore.Transaction) -> bool:
            snapshot = doc_ref.get(transaction=transaction)
            current = (snapshot.to_dict() or {}).get("count", 0) if snapshot.exists else 0
            if current >= settings.agent_gateway_rate_limit_per_minute:
                return False
            transaction.set(
                doc_ref,
                {
                    "count": current + 1,
                    "caller_identity": caller_identity,
                    "agent_id": agent_id,
                    "window": window,
                },
                merge=True,
            )
            return True

        within_limit = _run(self._db.transaction())
        if not within_limit:
            detail = (
                f"'{caller_identity}' excedeu {settings.agent_gateway_rate_limit_per_minute} "
                f"chamadas/minuto para '{agent_id}' (janela {window})"
            )
            self._audit(
                agent_id=agent_id,
                caller_identity=caller_identity,
                stage="rate_limit",
                outcome="REJECTED",
                error=detail,
                agent_version=manifest_version,
            )
            raise GatewayRejection("rate_limit", "rate_limit_exceeded", detail)

    # -- etapa 5 -------------------------------------------------------------

    def _authorize(self, agent_id: str, caller_identity: str, manifest_version: str) -> None:
        allowed = self._authorization_policy.get(agent_id, frozenset())
        if allowed is None or caller_identity in allowed:
            return

        if agent_id == _TAKEDOWN_AGENT_ID:
            # Mensagem dedicada, nao o "nao autorizado" generico -- ver
            # comentario de AUTHORIZATION_POLICY acima. NENHUM chamador
            # (nem dashboard-sa) e autorizado aqui, por design: o unico
            # caminho real e o fluxo humano do dashboard, que publica
            # diretamente em 'takedown-approved'.
            error = "human_approval_required_via_dashboard"
            detail = (
                "'takedown-agent' nao e invocavel via este gateway, para NENHUM "
                "chamador -- decisao arquitetural deliberada, nao uma restricao de "
                "identidade que se possa contornar com outra credencial. So pode ser "
                "acionado por uma aprovacao humana registrada no Firestore "
                "(approved_by/approved_at/decision_rationale), publicada em "
                "'takedown-approved' exclusivamente pelo fluxo do dashboard "
                "(dashboard-sa e a UNICA identidade com permissao de publish nesse "
                "topico -- ver infra/README.md)."
            )
        else:
            error = "not_authorized"
            detail = f"'{caller_identity}' nao esta autorizado a invocar '{agent_id}'"

        self._audit(
            agent_id=agent_id,
            caller_identity=caller_identity,
            stage="authorization",
            outcome="REJECTED",
            error=detail,
            agent_version=manifest_version,
        )
        raise GatewayRejection("authorization", error, detail)

    # -- etapa 6 -------------------------------------------------------------

    def _route(
        self, agent_id: str, payload: dict[str, Any], caller_identity: str, manifest_version: str
    ) -> tuple[str, str]:
        topic_id = self._routing_table.get(agent_id)
        if topic_id is None:
            detail = (
                f"'{agent_id}' nao tem topico de roteamento configurado -- nao e invocavel "
                "via gateway (ver AGENT_ROUTING_TOPIC em agent_gateway.py)"
            )
            self._audit(
                agent_id=agent_id,
                caller_identity=caller_identity,
                stage="routing",
                outcome="REJECTED",
                error=detail,
                agent_version=manifest_version,
            )
            raise GatewayRejection("routing", "not_routable", detail)

        topic_path = self._publisher.topic_path(settings.gcp_project_id, topic_id)
        try:
            future = self._publisher.publish(topic_path, data=json.dumps(payload).encode("utf-8"))
            message_id = future.result(timeout=10)
        except Exception as exc:  # noqa: BLE001 -- qualquer falha de publish e um erro de roteamento
            detail = f"falha ao publicar em '{topic_id}': {exc}"
            self._audit(
                agent_id=agent_id,
                caller_identity=caller_identity,
                stage="routing",
                outcome="REJECTED",
                error=detail,
                agent_version=manifest_version,
            )
            raise GatewayRejection("routing", "publish_failed", detail) from exc
        return topic_id, message_id

    # -- etapa 7 -------------------------------------------------------------

    def _audit(
        self,
        *,
        agent_id: str,
        caller_identity: str | None,
        stage: str,
        outcome: Literal["ALLOWED", "REJECTED"],
        error: str | None,
        agent_version: str | None,
    ) -> None:
        try:
            self._db.collection(settings.agent_gateway_audit_log_collection).add(
                {
                    "agent_id": agent_id,
                    "agent_version": agent_version,
                    "caller_identity": caller_identity,
                    "stage_reached": stage,
                    "outcome": outcome,
                    "error": error,
                    "created_at": self._now_fn(),
                }
            )
        except Exception:  # noqa: BLE001 -- falha ao auditar nunca deve mascarar o erro original
            logger.exception("Falha ao gravar log de auditoria do gateway (agent_id=%s)", agent_id)

    # -- orquestracao --------------------------------------------------------

    def handle_invocation(
        self,
        agent_id: str,
        raw_body: bytes,
        *,
        authorization_header: str | None,
        version: str | None = None,
    ) -> InvocationAccepted:
        """Roda as 7 etapas na ordem. Cada etapa ja audita a propria
        rejeicao antes de levantar `GatewayRejection` -- so o sucesso
        final e auditado aqui."""
        caller_identity = self._authenticate(authorization_header, agent_id)
        manifest = self._resolve(agent_id, version, caller_identity)
        payload = self._parse_payload(raw_body, manifest, caller_identity)
        self._validate_schema(payload, manifest, caller_identity)
        self._check_rate_limit(agent_id, caller_identity, manifest.version)
        self._authorize(agent_id, caller_identity, manifest.version)
        topic_id, message_id = self._route(agent_id, payload, caller_identity, manifest.version)

        self._audit(
            agent_id=agent_id,
            caller_identity=caller_identity,
            stage="routing",
            outcome="ALLOWED",
            error=None,
            agent_version=manifest.version,
        )
        return InvocationAccepted(
            agent_id=agent_id,
            agent_version=manifest.version,
            caller_identity=caller_identity,
            routed_to_topic=topic_id,
            message_id=message_id,
        )

    def authenticate_only(self, authorization_header: str | None) -> str:
        """Usado por GET /agents -- exige a mesma Agent Identity valida do
        pipeline de invocacao, mas nao passa pelas etapas 2-7 (listar o
        registry nao e uma invocacao)."""
        try:
            token = _extract_bearer_token(authorization_header)
            claims = self._verify_token(token)
            return claims["email"]
        except Exception as exc:  # noqa: BLE001
            raise GatewayRejection(
                "authentication", "invalid_or_missing_credentials", str(exc)
            ) from exc


# ---------------------------------------------------------------------------
# App FastAPI
# ---------------------------------------------------------------------------


def create_app(gateway: AgentGateway | None = None) -> FastAPI:
    """Fabrica do app -- permite injetar um `AgentGateway` de teste (fakes
    de Firestore/Pub/Sub/verificador de token) sem tocar rede real. Em
    producao (`gunicorn`/`uvicorn` apontando pra `agent_gateway:app`, ver
    Parte B), roda sem argumento e usa as dependencias reais."""
    if gateway is None:
        if settings.agent_gateway_audience is None:
            logger.warning(
                "AGENT_GATEWAY_AUDIENCE nao configurada -- ID tokens sao aceitos SEM checar "
                "audience (assinatura/expiracao ainda sao verificadas). Configure antes de "
                "expor este servico fora de desenvolvimento local."
            )
        gateway = AgentGateway()

    app = FastAPI(title="Sentinel Agent Gateway", version="1.0.0")

    @app.exception_handler(GatewayRejection)
    async def _handle_rejection(request: Request, exc: GatewayRejection) -> JSONResponse:
        agent_id = request.path_params.get("agent_id", "unknown")
        body = GatewayErrorBody(
            stage=exc.stage, error=exc.error, detail=exc.detail, agent_id=agent_id
        )
        return JSONResponse(
            status_code=_STAGE_HTTP_STATUS[exc.stage], content=body.model_dump(mode="json")
        )

    # NAO "/healthz" -- reproduzido em producao (Sprint 8, sessao de
    # validacao de 48h): o Google Frontend do Cloud Run intercepta esse
    # path especifico para o proprio probe de plataforma e a requisicao
    # NUNCA chega ao FastAPI (sem log nenhum deste processo, sem jeito de
    # depurar por dentro da aplicacao). "/readyz" nao tem esse conflito.
    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/agents", response_model=list[AgentSummary])
    async def list_agents(request: Request) -> list[AgentSummary]:
        # Firestore e um cliente sincrono/bloqueante -- roda numa thread
        # separada para nunca travar o event loop (regra do CLAUDE.md),
        # mesmo padrao ja usado em orchestrator.py/takedown_agent.py.
        await asyncio.to_thread(gateway.authenticate_only, request.headers.get("authorization"))
        manifests = await asyncio.to_thread(registry.list_agents)
        return [
            AgentSummary(
                agent_id=m.agent_id,
                version=m.version,
                status=m.status.value,
                owner_team=m.owner_team,
                description=m.description,
                sla_seconds=m.sla_seconds,
                required_permissions=m.required_permissions,
                routable=m.agent_id in AGENT_ROUTING_TOPIC,
                input_schema=m.input_schema,
                output_schema=m.output_schema,
            )
            for m in manifests
        ]

    @app.post("/invoke/{agent_id}", response_model=InvocationAccepted)
    async def invoke(agent_id: str, request: Request, version: str | None = None) -> InvocationAccepted:
        raw_body = await request.body()
        # Todo o pipeline (Firestore + Pub/Sub, sincronos/bloqueantes) roda
        # numa thread separada -- nunca bloqueia o event loop.
        return await asyncio.to_thread(
            gateway.handle_invocation,
            agent_id,
            raw_body,
            authorization_header=request.headers.get("authorization"),
            version=version,
        )

    return app


app = create_app()
