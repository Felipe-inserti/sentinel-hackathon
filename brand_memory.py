"""Camada 7 -- Memory Bank Adaptativo (Sprint 7, Parte B).

Requisito da trilha: "contexto seguro e persistente ao longo de semanas".
Ate este sprint, uma rejeicao humana ("falso positivo") ou uma aprovacao de
takedown ficavam gravadas em `investigations/{domain}` (ver
`dashboard/.../review/actions.ts`) mas nunca alimentavam nenhuma
investigacao FUTURA -- o mesmo erro podia se repetir indefinidamente para
padroes parecidos da mesma marca. `brand_memory.py` fecha esse loop: toda
decisao humana terminal vira uma `MemoryEntry` imutavel e datada em
`brand_memory`, e as N entradas mais relevantes para a marca sao injetadas
como few-shot no prompt da proxima investigacao daquela marca (ver
`plane2_agents/orchestrator.py::classify_domain_with_gemini`) -- sem
retreinar nada, so contexto adicional na chamada seguinte.

## Como uma decisao vira memoria

Duas origens, mesma funcao de gravacao (`_record_memory_entry`):

  - `record_rejection`: a partir de um dossie `investigations/{domain}`
    com `status == "REJECTED"` (rejeicao humana = falso positivo
    confirmado -- o modelo disse MALICIOUS, um humano confirmou que nao
    era).
  - `record_approval`: a partir de um dossie com
    `status == "TAKEDOWN_APPROVED"` (aprovacao humana = verdadeiro
    positivo confirmado).

Quem chama essas duas funcoes com o dossie real e `sync_brand_memory.py`
(varredura periodica, ver seu docstring sobre por que pull e nao push
neste sprint) ou, para a demonstracao, `replay_investigation.py`.

## "Uma rejeicao humana nao santifica o texto"

Todo texto que entra numa `MemoryEntry` -- o `reasoning` original do LLM
(que pode ecoar a pagina raspada) e a justificativa digitada pelo humano
(`rejection_reason`/`decision_rationale`, que um revisor distraido pode ter
colado direto da pagina) -- passa por `sanitizer.sanitize` ANTES de
persistir. Um humano aprovando/rejeitando e uma decisao de negocio, nao uma
barreira de sanitizacao (CLAUDE.md #1: conteudo vindo de um site suspeito e
adversarial por definicao, mesmo depois de passar por um humano). Sem
isso, `brand_memory` seria uma porta dos fundos para injetar prompt
injection direto no PROXIMO prompt de sistema.

## Isolamento entre marcas

Mesma garantia de `brand_agent.BrandScopedInvestigations` (ver seu
docstring): toda leitura de `brand_memory` para uma marca e filtrada NA
QUERY do Firestore por `brand_id`, nunca em memoria depois -- ver
`BrandScopedMemory` abaixo.

## Persistencia versionada e datada

`MemoryEntry` nunca e sobrescrita: o `doc_id` e deterministico
(`{brand_id}__{domain}__{decision_type}__{decided_at_isoformat}`, ver
`_memory_doc_id`), entao a MESMA decisao sincronizada de novo (ex:
`sync_brand_memory.py` rodado repetidas vezes) e idempotente, mas uma
decisao NOVA sobre o mesmo dominio (`decided_at` diferente) sempre vira uma
entrada adicional, nunca substitui a anterior -- `memory_version` numera
essa sequencia e `created_at` data cada entrada, provando persistencia ao
longo de semanas sem apagar historico.

## Custo (tese de token economy)

Few-shot aumenta tokens de ENTRADA por investigacao -- trabalha CONTRA a
tese central do projeto (CLAUDE.md: "se uma feature aumenta chamadas de
LLM sem ganho proporcional, ela esta errada" -- aqui nao aumenta CHAMADAS,
mas aumenta TAMANHO da chamada, mesmo risco de custo). Por isso:
  - o numero de exemplos injetados e configuravel
    (`settings.brand_memory_max_examples`; 0 desliga a injecao inteira e
    evita ate a leitura extra no Firestore, ver `get_relevant_memories`);
  - todo bloco de few-shot injetado tem seu custo ESTIMADO (heuristica de
    caracteres/token, ver `estimate_extra_tokens` -- nunca uma medicao real
    de tokenizador, mesma disciplina de aproximacao ja documentada em
    `config.py` para o preco do Cloud Run) e registrado em telemetria
    (`telemetry.py::_COUNTER_NAMES`, prefixo `brand_memory_`) -- o
    trade-off fica visivel para decisao humana, nunca escondido.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal

import Levenshtein
from google.cloud import firestore
from google.cloud.firestore import FieldFilter
from pydantic import BaseModel

from config import settings
from plane1_ingestion.prefilter import normalize_domain
from sanitizer import sanitize

logger = logging.getLogger("brand_memory")

db = firestore.Client()

DecisionType = Literal["REJECTED_FALSE_POSITIVE", "APPROVED_TRUE_POSITIVE"]

# Heuristica de estimativa de tokens SEM chamar nenhuma API de
# tokenizacao real (custaria uma chamada extra so pra medir) -- ~4
# caracteres por token e a aproximacao padrao usada pela industria para
# textos em portugues/ingles latino. Documentado, nunca escondido (ver
# docstring do modulo).
_CHARS_PER_TOKEN_ESTIMATE = 4


class MemoryEntry(BaseModel):
    """Uma entrada IMUTAVEL do banco de memoria de uma marca -- ver
    docstring do modulo sobre persistencia versionada e datada."""

    brand_id: str
    domain: str
    decision_type: DecisionType
    original_classification: str
    original_confidence: float
    original_reasoning: str  # sanitizado, ver _record_memory_entry
    human_decided_by: str
    human_decided_at: datetime
    human_rationale: str  # sanitizado, ver _record_memory_entry
    memory_version: int = 1
    created_at: datetime  # quando ESTA entrada foi gravada em brand_memory

    def as_few_shot_line(self, index: int) -> str:
        """Uma linha do bloco few-shot injetado no prompt (ver
        `orchestrator.classify_domain_with_gemini`) -- so campos ja
        sanitizados no momento da gravacao, nunca reconstroi a partir de
        dado bruto."""
        veredito = (
            "SAFE (falso positivo confirmado por revisor humano)"
            if self.decision_type == "REJECTED_FALSE_POSITIVE"
            else "MALICIOUS (verdadeiro positivo confirmado por revisor humano)"
        )
        return (
            f"{index}. dominio anterior: {self.domain} | veredito correto: {veredito} | "
            f"classificacao original do modelo: {self.original_classification} "
            f"(confianca {self.original_confidence:.2f}) | "
            f"justificativa humana: {self.human_rationale}"
        )


def _memory_doc_id(brand_id: str, domain: str, decision_type: str, decided_at: datetime) -> str:
    """Doc ID deterministico -- ver docstring do modulo sobre
    idempotencia/versionamento."""
    return f"{brand_id}__{domain}__{decision_type}__{decided_at.isoformat()}"


def _default_collection() -> firestore.CollectionReference:
    return db.collection(settings.brand_memory_collection)


class BrandScopedMemory:
    """Unico caminho para ler `brand_memory` de uma marca -- mesma
    garantia de isolamento de `brand_agent.BrandScopedInvestigations` (ver
    docstring do modulo): o filtro por `brand_id` roda NA QUERY do
    Firestore, nunca em memoria depois de trazer a colecao inteira."""

    def __init__(
        self, brand_id: str, collection_ref: firestore.CollectionReference | None = None
    ) -> None:
        self._brand_id = brand_id
        self._collection_ref = collection_ref if collection_ref is not None else _default_collection()

    def list_all(self, *, limit: int = 500) -> list[MemoryEntry]:
        query = self._collection_ref.where(filter=FieldFilter("brand_id", "==", self._brand_id)).limit(limit)
        return [MemoryEntry.model_validate(doc.to_dict()) for doc in query.stream()]


def _next_memory_version(
    brand_id: str, domain: str, collection_ref: firestore.CollectionReference
) -> int:
    existing = list(
        collection_ref.where(filter=FieldFilter("brand_id", "==", brand_id))
        .where(filter=FieldFilter("domain", "==", domain))
        .stream()
    )
    return len(existing) + 1


def _parse_decision_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise ValueError(f"Nao foi possivel interpretar data de decisao humana: {value!r}")


def _record_memory_entry(
    *,
    brand_id: str,
    domain: str,
    decision_type: DecisionType,
    original_classification: str,
    original_confidence: float,
    original_reasoning: str,
    human_decided_by: str,
    human_decided_at: datetime,
    human_rationale: str,
    collection_ref: firestore.CollectionReference | None = None,
) -> MemoryEntry:
    ref = collection_ref if collection_ref is not None else _default_collection()

    # "Uma rejeicao humana nao santifica o texto" -- ver docstring do
    # modulo. Sanitiza tanto o reasoning do modelo quanto o texto digitado
    # pelo humano antes de persistir qualquer coisa.
    clean_reasoning = sanitize(original_reasoning).clean_text
    clean_rationale = sanitize(human_rationale).clean_text

    version = _next_memory_version(brand_id, domain, ref)
    entry = MemoryEntry(
        brand_id=brand_id,
        domain=domain,
        decision_type=decision_type,
        original_classification=original_classification,
        original_confidence=original_confidence,
        original_reasoning=clean_reasoning,
        human_decided_by=human_decided_by,
        human_decided_at=human_decided_at,
        human_rationale=clean_rationale,
        memory_version=version,
        created_at=datetime.now(timezone.utc),
    )
    doc_id = _memory_doc_id(brand_id, domain, decision_type, human_decided_at)
    ref.document(doc_id).set(entry.model_dump(mode="python"))
    logger.info(
        "Memoria registrada: marca=%s dominio=%s tipo=%s versao=%d",
        brand_id,
        domain,
        decision_type,
        version,
    )
    return entry


def record_rejection(
    *, brand_id: str, domain: str, investigation: dict[str, Any], collection_ref: firestore.CollectionReference | None = None
) -> MemoryEntry:
    """Constroi uma `MemoryEntry` `REJECTED_FALSE_POSITIVE` a partir de um
    documento `investigations/{domain}` com `status == "REJECTED"` (ver
    `dashboard/.../review/actions.ts::rejectInvestigation`). Nao valida o
    `status` aqui -- quem decide QUANDO chamar isso e o chamador
    (`sync_brand_memory.py`/`replay_investigation.py`), esta funcao so
    constroi a entrada a partir do dossie ja fornecido."""
    return _record_memory_entry(
        brand_id=brand_id,
        domain=domain,
        decision_type="REJECTED_FALSE_POSITIVE",
        original_classification=investigation.get("classification", "UNKNOWN"),
        original_confidence=float(investigation.get("confidence", 0.0)),
        original_reasoning=investigation.get("reasoning", ""),
        human_decided_by=investigation.get("rejected_by") or "desconhecido",
        human_decided_at=_parse_decision_datetime(investigation.get("rejected_at")),
        human_rationale=investigation.get("rejection_reason", ""),
        collection_ref=collection_ref,
    )


def record_approval(
    *, brand_id: str, domain: str, investigation: dict[str, Any], collection_ref: firestore.CollectionReference | None = None
) -> MemoryEntry:
    """Constroi uma `MemoryEntry` `APPROVED_TRUE_POSITIVE` a partir de um
    documento `investigations/{domain}` com
    `status == "TAKEDOWN_APPROVED"` (ver
    `dashboard/.../review/actions.ts::approveTakedown`)."""
    return _record_memory_entry(
        brand_id=brand_id,
        domain=domain,
        decision_type="APPROVED_TRUE_POSITIVE",
        original_classification=investigation.get("classification", "UNKNOWN"),
        original_confidence=float(investigation.get("confidence", 0.0)),
        original_reasoning=investigation.get("reasoning", ""),
        human_decided_by=investigation.get("approved_by") or "desconhecido",
        human_decided_at=_parse_decision_datetime(investigation.get("approved_at")),
        human_rationale=investigation.get("decision_rationale", ""),
        collection_ref=collection_ref,
    )


def get_relevant_memories(brand_id: str, domain: str, *, limit: int) -> list[MemoryEntry]:
    """As `limit` entradas mais relevantes desta marca para injetar como
    few-shot (mistura REJECTED_FALSE_POSITIVE e APPROVED_TRUE_POSITIVE --
    ver docstring do modulo, itens 6/7 do sprint). Relevancia =
    similaridade (`Levenshtein.ratio`, zero custo de LLM, mesma matematica
    do prefiltro) entre o dominio normalizado investigado agora e o
    dominio de cada memoria, desempate por recencia
    (`created_at` mais novo primeiro).

    `limit <= 0` devolve lista vazia SEM nenhuma leitura no Firestore --
    desliga a injecao inteira, inclusive o custo de uma query extra (ver
    docstring do modulo sobre token economy)."""
    if limit <= 0:
        return []

    all_entries = BrandScopedMemory(brand_id).list_all()
    if not all_entries:
        return []

    target = normalize_domain(domain)

    def _relevance_key(entry: MemoryEntry) -> tuple[float, datetime]:
        return (Levenshtein.ratio(target, normalize_domain(entry.domain)), entry.created_at)

    ranked = sorted(all_entries, key=_relevance_key, reverse=True)
    return ranked[:limit]


def estimate_extra_tokens(few_shot_block_text: str) -> int:
    """Estimativa aproximada de tokens de ENTRADA adicionados pelo bloco
    few-shot (heuristica de caracteres/token, ver docstring do modulo) --
    NUNCA chama nenhuma API de tokenizacao real."""
    if not few_shot_block_text:
        return 0
    return max(1, len(few_shot_block_text) // _CHARS_PER_TOKEN_ESTIMATE)
