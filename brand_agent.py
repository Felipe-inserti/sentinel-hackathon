"""Camada 6 -- BrandAgent: contexto e isolamento por marca cliente.

Requisito da trilha (Fortified Enterprise Fleet): "rede escalavel de
agentes institucionais" e "contexto seguro e persistente ao longo de
semanas". Ate este sprint, o Sentinel tratava toda investigacao com o
MESMO prompt/limiar, independente da marca alvo -- `matched_brand` so
existia como um rotulo de texto, sem contexto proprio nem consequencia
na decisao de escalonar. Um `BrandAgent` e uma instancia por marca cliente
(Nubank, Loggi, iFood...) que carrega esse contexto: dominios legitimos
conhecidos, padroes de typosquatting ja observados, contatos de abuso,
tolerancia a risco e o limiar de confianca que decide se aquela marca
especifica exige revisao humana.

## Contrato publicado vs. dado mutavel

Um BrandAgent tem DUAS metades, deliberadamente separadas (mesmo principio
que ja existe entre `registry.py` e `investigations`):

  1. `AgentManifest` publicado em `agent_registry` (ver `registry.py`) sob
     `agent_id = "brand-agent-{brand_id}"` -- o CONTRATO versionado
     (input/output schema, permissoes, SLA). Publicado por
     `seed_brand_agents.py`, descoberto e validado via
     `registry.invoke_agent`, exatamente como `orchestrator`/
     `evidence-collector`/`takedown-agent` -- NENHUM caminho paralelo de
     invocacao (regra explicita do sprint).
  2. `BrandContext` persistido em `brand_context/{brand_id}` -- o DADO
     mutavel (listas de dominios/padroes/contatos crescem com o tempo,
     limiares podem ser ajustados). Nao faz parte do manifesto porque
     `AgentManifest` e imutavel por construcao (ver `registry.py`); um
     limiar de risco mudando toda semana nao deveria forcar publicar uma
     versao semver nova do contrato.

`discover_brand_agent` e o UNICO ponto que junta as duas metades e devolve
um `BrandAgent` pronto para uso -- ver sua docstring.

## Isolamento de dados entre marcas (o argumento de data sovereignty)

Firestore nao tem IAM por colecao (mesma limitacao ja documentada nos
Sprints 3 e 6 -- ver `takedown_agent.ReadOnlyCollectionAccess`): qualquer
credencial de servico com `roles/datastore.user` pode, em teoria, ler
`investigations` inteira. A garantia real aqui e, com a MESMA honestidade
de narrativa do takedown-sa, de APLICACAO, nao de infraestrutura:
`BrandScopedInvestigations` e o UNICO objeto que um `BrandAgent` usa para
ler `investigations`, e:

  - toda query em lote ja sai filtrada em codigo por
    `matched_brand == brand_id` NO NIVEL DA QUERY (`.where(...)` do
    Firestore), nao um filtro em memoria depois de trazer tudo;
  - todo lookup por dominio unico reconfirma `matched_brand` no documento
    devolvido antes de entregar ao chamador -- um BrandAgent do Itau nunca
    devolve, nem por engano, um dossie cujo `matched_brand` seja "nubank";
    uma tentativa levanta `BrandIsolationViolation` (auditavel, nunca
    silenciosa).

Ver `tests/test_brand_agent.py` para os testes que FALHAM caso essa
garantia quebre.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal

from google.cloud import firestore
from pydantic import BaseModel, Field

import registry
from config import settings

logger = logging.getLogger("brand_agent")

db = firestore.Client()


class BrandContext(BaseModel):
    """Contexto operacional mutavel de uma marca cliente. Persistido em
    `brand_context/{brand_id}` (ver `settings.brand_context_collection`) --
    ver docstring do modulo sobre por que isso NAO faz parte do
    `AgentManifest` publicado."""

    brand_id: str
    display_name: str
    legitimate_domains: list[str] = Field(default_factory=list)
    known_typosquat_patterns: list[str] = Field(default_factory=list)
    abuse_contacts: list[str] = Field(default_factory=list)
    risk_tolerance: Literal["LOW", "MEDIUM", "HIGH"] = "MEDIUM"
    # Abaixo deste valor de confianca, um veredito MALICIOUS desta marca
    # exige revisao humana mesmo sem nenhum sinal de tentativa de injecao
    # (ver BrandAgent.should_escalate) -- marcas com tolerancia a risco mais
    # baixa devem publicar um limiar mais alto.
    confidence_escalation_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    created_at: datetime
    updated_at: datetime


class BrandAgentNotConfiguredError(LookupError):
    """Nao existe `BrandContext` publicado para essa marca em Firestore --
    tratado como 'sem contexto de marca disponivel', nunca como um default
    inventado silenciosamente (mesma disciplina de `config.py`)."""


class BrandIsolationViolation(RuntimeError):
    """Levantado quando um lookup por dominio unico devolveria um dossie de
    marca diferente da do `BrandAgent` que pediu -- nunca deveria acontecer
    através da API publica de `BrandScopedInvestigations` (que so aceita
    dominios que o proprio chamador ja acredita pertencerem a marca), mas
    existe como defesa em profundidade auditavel, nao uma falha silenciosa
    que devolveria dado da marca errada."""


def agent_id_for_brand(brand_id: str) -> str:
    """`brand_id` (ex: "nubank") -> `agent_id` no Agent Registry (ex:
    "brand-agent-nubank"). Unica formula usada tanto por
    `seed_brand_agents.py` (publicacao) quanto por `discover_brand_agent`
    (descoberta) -- nunca hard-coded em mais de um lugar."""
    return f"brand-agent-{brand_id}"


def _brand_context_doc(brand_id: str) -> firestore.DocumentReference:
    return db.collection(settings.brand_context_collection).document(brand_id)


def get_brand_context(brand_id: str) -> BrandContext:
    """Busca o `BrandContext` publicado. Levanta `BrandAgentNotConfiguredError`
    se nao existir -- nunca inventa um contexto default."""
    doc = _brand_context_doc(brand_id).get()
    if not doc.exists:
        raise BrandAgentNotConfiguredError(
            f"Nenhum BrandContext publicado para a marca '{brand_id}' em "
            f"'{settings.brand_context_collection}/{brand_id}'"
        )
    return BrandContext.model_validate(doc.to_dict())


def publish_brand_context(context: BrandContext) -> BrandContext:
    """Publica/atualiza o `BrandContext` de uma marca. Idempotente (mesmo
    padrao de `registry.publish_agent`): sobrescreve o documento, mas
    preserva o `created_at` original se o doc ja existir -- so
    `updated_at` deve refletir o momento desta chamada (o chamador e
    responsavel por passar o `updated_at` correto; esta funcao nao o
    recalcula para permanecer testavel sem depender do relogio de
    parede)."""
    doc_ref = _brand_context_doc(context.brand_id)
    existing = doc_ref.get()
    if existing.exists:
        existing_data = existing.to_dict() or {}
        preserved_created_at = existing_data.get("created_at", context.created_at)
        context = context.model_copy(update={"created_at": preserved_created_at})
    doc_ref.set(context.model_dump(mode="python"))
    logger.info("BrandContext publicado/atualizado para '%s'", context.brand_id)
    return context


class BrandScopedInvestigations:
    """Unico caminho pelo qual um `BrandAgent` le a colecao `investigations`
    -- ver docstring do modulo sobre a garantia de isolamento. Nunca
    exponha `self._collection_ref` nem `db` diretamente a partir de um
    `BrandAgent`."""

    def __init__(
        self,
        brand_id: str,
        collection_ref: firestore.CollectionReference | None = None,
    ) -> None:
        self._brand_id = brand_id
        self._collection_ref = collection_ref if collection_ref is not None else db.collection(
            settings.firestore_collection
        )

    def list_recent(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """Toda investigacao devolvida ja pertence a esta marca -- o
        filtro roda NO FIRESTORE (`.where`), nao em memoria depois de
        trazer a colecao inteira. Usado por `brand_memory.py` (Parte B)
        para construir o banco de memoria few-shot so com dado da propria
        marca."""
        query = self._collection_ref.where("matched_brand", "==", self._brand_id).limit(limit)
        return [doc.to_dict() for doc in query.stream()]

    def list_by_status(self, statuses: list[str], *, limit: int = 500) -> list[dict[str, Any]]:
        """Mesma garantia de `list_recent`, com um segundo filtro por
        `status` (ex: `["REJECTED", "TAKEDOWN_APPROVED"]`) tambem NA
        QUERY. Usado por `sync_brand_memory.py` para achar decisoes
        humanas terminais desta marca ainda nao espelhadas em
        `brand_memory` -- unico caminho, nunca uma query solta em outro
        modulo (mesma disciplina de `list_recent`)."""
        query = (
            self._collection_ref.where("matched_brand", "==", self._brand_id)
            .where("status", "in", statuses)
            .limit(limit)
        )
        return [doc.to_dict() for doc in query.stream()]

    def get(self, domain: str) -> dict[str, Any] | None:
        """Lookup por dominio unico. Devolve `None` se o dominio nao tem
        dossie. Se o dossie existir mas pertencer a OUTRA marca, nunca o
        devolve -- levanta `BrandIsolationViolation` (auditavel) em vez de
        um `None` silencioso, porque esse caso especifico e o unico em que
        o chamador pediu por um dominio que ele mesmo acreditava ser da
        sua marca e estava errado (bug do chamador ou tentativa de acesso
        indevido), distinto de "dominio nunca investigado"."""
        doc = self._collection_ref.document(domain).get()
        if not doc.exists:
            return None
        data = doc.to_dict() or {}
        if data.get("matched_brand") != self._brand_id:
            logger.error(
                "Isolamento de marca violado: BrandAgent '%s' tentou ler dossie de '%s' "
                "(marca real do dossie: %s) -- acesso negado.",
                self._brand_id,
                domain,
                data.get("matched_brand"),
            )
            raise BrandIsolationViolation(
                f"Dossie de '{domain}' pertence a marca '{data.get('matched_brand')}', "
                f"nao a '{self._brand_id}'"
            )
        return data


class BrandAgent:
    """Agente de marca: contexto operacional + acesso isolado ao historico
    de investigacoes da propria marca. So deve ser construido via
    `discover_brand_agent` (nunca instanciado direto pelo orquestrador) --
    ver docstring do modulo."""

    def __init__(self, context: BrandContext, agent_manifest: registry.AgentManifest) -> None:
        self.context = context
        self.agent_manifest = agent_manifest
        self.investigations = BrandScopedInvestigations(context.brand_id)

    @property
    def brand_id(self) -> str:
        return self.context.brand_id

    def should_escalate(self, classification: str, confidence: float) -> bool:
        """Sinal de escalonamento ESPECIFICO da marca, adicional ao sinal
        de injecao ja aplicado por `orchestrator._save_investigation`
        (independente um do outro -- os dois sao combinados com OR pelo
        chamador). So se aplica a MALICIOUS: uma marca com tolerancia a
        risco baixa nao precisa reconfirmar um SAFE, so quer ter certeza
        antes de aceitar automaticamente um MALICIOUS de baixa confianca."""
        if classification != "MALICIOUS":
            return False
        return confidence < self.context.confidence_escalation_threshold


class BrandRoutingRequest(BaseModel):
    """`input_schema` publicado para cada `brand-agent-{brand_id}` no
    Agent Registry -- payload minimo que o orquestrador usa para rotear
    (mesmo espirito de `SuspiciousDomainSignal` em `seed_registry.py`)."""

    domain: str
    matched_brand: str


class BrandGuidance(BaseModel):
    """`output_schema` publicado -- metadados de decisao que o BrandAgent
    expoe sobre si mesmo (nunca PII, nunca dado de outra marca)."""

    brand_id: str
    confidence_escalation_threshold: float
    risk_tolerance: str


def discover_brand_agent(brand_id: str, domain: str) -> BrandAgent | None:
    """Ponto UNICO de descoberta + roteamento para um BrandAgent (regra
    explicita do sprint: nenhum caminho paralelo de invocacao). Resolve e
    valida via `registry.invoke_agent` -- exatamente o mesmo mecanismo que
    `orchestrator.py`/`evidence_agent.py`/`takedown_agent.py` usam para se
    descobrirem -- e carrega o `BrandContext` associado.

    Devolve `None` (nunca levanta) quando a marca nao tem um BrandAgent
    ACTIVE publicado, ou tem o manifesto mas nao tem `BrandContext`
    configurado: rotear para um BrandAgent e um MELHORAMENTO opcional da
    investigacao, nao um requisito -- um dominio de marca ainda nao
    cadastrada como BrandAgent deve continuar sendo investigado com o
    comportamento generico anterior a este sprint, nunca falhar a
    investigacao inteira por causa disso."""
    agent_id = agent_id_for_brand(brand_id)
    payload = BrandRoutingRequest(domain=domain, matched_brand=brand_id).model_dump()

    try:
        manifest = registry.invoke_agent(agent_id, payload)
    except (registry.AgentNotFoundError, registry.AgentInvocationError) as exc:
        logger.info(
            "Sem BrandAgent ativo para a marca '%s' (%s) -- investigacao de %s segue sem contexto de marca",
            brand_id,
            exc,
            domain,
        )
        return None

    try:
        context = get_brand_context(brand_id)
    except BrandAgentNotConfiguredError:
        logger.warning(
            "BrandAgent '%s' publicado no registry mas sem BrandContext em Firestore -- tratado como indisponivel",
            agent_id,
        )
        return None

    return BrandAgent(context=context, agent_manifest=manifest)
