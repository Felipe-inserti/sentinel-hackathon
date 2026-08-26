"""Testes de `brand_agent.py` -- Firestore e Agent Registry sempre
mockados (nenhuma chamada real). Foco no criterio de aceite do sprint:

  "Isolamento entre marcas... escreva testes que FALHEM se um BrandAgent
  conseguir ler dossie de outra marca."

Ver `test_brand_isolation_get_blocks_cross_brand_document` e
`test_brand_isolation_list_recent_never_returns_other_brand` abaixo -- sao
os dois testes que devem FALHAR (dossie vazando) se a garantia de
isolamento em `BrandScopedInvestigations` for removida ou enfraquecida."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import brand_agent as ba
import registry


def _context(
    brand_id: str = "nubank",
    *,
    threshold: float = 0.7,
    risk_tolerance: str = "MEDIUM",
) -> ba.BrandContext:
    now = datetime.now(timezone.utc)
    return ba.BrandContext(
        brand_id=brand_id,
        display_name=brand_id.title(),
        legitimate_domains=[f"{brand_id}.com.br"],
        known_typosquat_patterns=[],
        abuse_contacts=[f"seguranca@{brand_id}.com.br"],
        risk_tolerance=risk_tolerance,
        confidence_escalation_threshold=threshold,
        created_at=now,
        updated_at=now,
    )


def _manifest(agent_id: str = "brand-agent-nubank") -> registry.AgentManifest:
    return registry.AgentManifest(
        agent_id=agent_id,
        version="1.0.0",
        owner_team="sentinel-brand-ops",
        description="teste",
        input_schema=ba.BrandRoutingRequest.model_json_schema(),
        output_schema=ba.BrandGuidance.model_json_schema(),
        tools_allowed=["firestore.read"],
        required_permissions=["roles/datastore.user"],
        sla_seconds=2.0,
        status=registry.AgentStatus.ACTIVE,
        created_at=datetime.now(timezone.utc),
    )


# --- BrandContext ----------------------------------------------------------


def test_brand_context_rejects_threshold_out_of_range():
    with pytest.raises(Exception):
        _context(threshold=1.5)


# --- publish_brand_context / get_brand_context (fake Firestore) -----------


class _FakeDocSnapshot:
    def __init__(self, data: dict | None):
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict | None:
        return dict(self._data) if self._data is not None else None


class _FakeDocRef:
    def __init__(self, store: dict[str, dict], doc_id: str):
        self._store = store
        self._id = doc_id

    def get(self) -> _FakeDocSnapshot:
        return _FakeDocSnapshot(self._store.get(self._id))

    def set(self, data: dict) -> None:
        self._store[self._id] = dict(data)


class _FakeCollection:
    def __init__(self, store: dict[str, dict]):
        self._store = store

    def document(self, doc_id: str) -> _FakeDocRef:
        return _FakeDocRef(self._store, doc_id)


class _FakeFirestoreClient:
    def __init__(self):
        self._collections: dict[str, dict[str, dict]] = {}

    def collection(self, name: str) -> _FakeCollection:
        return _FakeCollection(self._collections.setdefault(name, {}))


@pytest.fixture()
def fake_db(monkeypatch):
    fake = _FakeFirestoreClient()
    monkeypatch.setattr(ba, "db", fake)
    return fake


def test_publish_and_get_brand_context_roundtrip(fake_db):
    ba.publish_brand_context(_context("nubank"))

    fetched = ba.get_brand_context("nubank")
    assert fetched.brand_id == "nubank"
    assert fetched.confidence_escalation_threshold == 0.7


def test_get_brand_context_missing_raises_not_configured(fake_db):
    with pytest.raises(ba.BrandAgentNotConfiguredError):
        ba.get_brand_context("marca-inexistente")


def test_publish_brand_context_preserves_original_created_at(fake_db):
    original = _context("nubank")
    ba.publish_brand_context(original)

    later = original.model_copy(
        update={"updated_at": datetime.now(timezone.utc), "risk_tolerance": "HIGH"}
    )
    # simula "created_at" diferente vindo de uma republicacao -- nao deve
    # sobrescrever o original.
    later = later.model_copy(update={"created_at": datetime.now(timezone.utc)})
    ba.publish_brand_context(later)

    fetched = ba.get_brand_context("nubank")
    assert fetched.created_at == original.created_at
    assert fetched.risk_tolerance == "HIGH"


# --- agent_id_for_brand ------------------------------------------------


def test_agent_id_for_brand_formula():
    assert ba.agent_id_for_brand("nubank") == "brand-agent-nubank"


# --- BrandAgent.should_escalate ------------------------------------------


def test_should_escalate_false_for_safe_classification():
    agent = ba.BrandAgent(context=_context(threshold=0.9), agent_manifest=_manifest())
    assert agent.should_escalate("SAFE", 0.1) is False


def test_should_escalate_true_when_confidence_below_brand_threshold():
    agent = ba.BrandAgent(context=_context(threshold=0.85), agent_manifest=_manifest())
    assert agent.should_escalate("MALICIOUS", 0.8) is True


def test_should_escalate_false_when_confidence_meets_brand_threshold():
    agent = ba.BrandAgent(context=_context(threshold=0.7), agent_manifest=_manifest())
    assert agent.should_escalate("MALICIOUS", 0.95) is False


# --- discover_brand_agent: unico ponto de descoberta+roteamento ----------


def test_discover_brand_agent_returns_none_without_active_registry_entry(monkeypatch):
    monkeypatch.setattr(
        ba.registry, "invoke_agent", MagicMock(side_effect=registry.AgentNotFoundError("nao existe"))
    )
    assert ba.discover_brand_agent("nubank", "nubank-fake.com") is None


def test_discover_brand_agent_returns_none_without_brand_context(monkeypatch):
    monkeypatch.setattr(ba.registry, "invoke_agent", MagicMock(return_value=_manifest()))
    monkeypatch.setattr(
        ba, "get_brand_context", MagicMock(side_effect=ba.BrandAgentNotConfiguredError("sem contexto"))
    )
    assert ba.discover_brand_agent("nubank", "nubank-fake.com") is None


def test_discover_brand_agent_success_returns_configured_agent(monkeypatch):
    manifest = _manifest()
    context = _context("nubank", threshold=0.85)
    monkeypatch.setattr(ba.registry, "invoke_agent", MagicMock(return_value=manifest))
    monkeypatch.setattr(ba, "get_brand_context", MagicMock(return_value=context))

    agent = ba.discover_brand_agent("nubank", "nubank-fake.com")

    assert agent is not None
    assert agent.brand_id == "nubank"
    assert agent.agent_manifest is manifest
    assert agent.context is context


def test_discover_brand_agent_invokes_registry_with_expected_payload(monkeypatch):
    fake_invoke = MagicMock(return_value=_manifest())
    monkeypatch.setattr(ba.registry, "invoke_agent", fake_invoke)
    monkeypatch.setattr(ba, "get_brand_context", MagicMock(return_value=_context("nubank")))

    ba.discover_brand_agent("nubank", "nubank-fake.com")

    fake_invoke.assert_called_once_with(
        "brand-agent-nubank", {"domain": "nubank-fake.com", "matched_brand": "nubank"}
    )


# --- Isolamento de dados entre marcas (criterio de aceite central) --------


def test_brand_isolation_get_blocks_cross_brand_document():
    """O agente do Itau nunca le um dossie cujo matched_brand seja
    'nubank' -- mesmo pedindo pelo dominio exato. FALHA se
    BrandScopedInvestigations devolver o dado em vez de recusar."""
    fake_collection = MagicMock()
    fake_doc_snapshot = MagicMock(exists=True)
    fake_doc_snapshot.to_dict.return_value = {"domain": "nubank-fake.com", "matched_brand": "nubank"}
    fake_collection.document.return_value.get.return_value = fake_doc_snapshot

    itau_scope = ba.BrandScopedInvestigations("itau", collection_ref=fake_collection)

    with pytest.raises(ba.BrandIsolationViolation):
        itau_scope.get("nubank-fake.com")


def test_brand_isolation_get_allows_same_brand_document():
    fake_collection = MagicMock()
    fake_doc_snapshot = MagicMock(exists=True)
    fake_doc_snapshot.to_dict.return_value = {"domain": "nubank-fake.com", "matched_brand": "nubank"}
    fake_collection.document.return_value.get.return_value = fake_doc_snapshot

    nubank_scope = ba.BrandScopedInvestigations("nubank", collection_ref=fake_collection)

    result = nubank_scope.get("nubank-fake.com")
    assert result is not None
    assert result["matched_brand"] == "nubank"


def test_brand_isolation_get_missing_document_returns_none():
    fake_collection = MagicMock()
    fake_collection.document.return_value.get.return_value = MagicMock(exists=False)

    scope = ba.BrandScopedInvestigations("nubank", collection_ref=fake_collection)
    assert scope.get("nunca-investigado.test") is None


def test_brand_isolation_list_recent_filters_at_query_level():
    """O filtro por marca precisa acontecer NA QUERY do Firestore
    (.where), nao so em memoria depois -- FALHA se list_recent passar a
    trazer a colecao inteira sem filtrar."""
    fake_collection = MagicMock()
    fake_query = MagicMock()
    fake_collection.where.return_value = fake_query
    fake_query.limit.return_value = fake_query
    fake_query.stream.return_value = []

    scope = ba.BrandScopedInvestigations("itau", collection_ref=fake_collection)
    scope.list_recent(limit=50)

    fake_collection.where.assert_called_once_with("matched_brand", "==", "itau")
    fake_query.limit.assert_called_once_with(50)


def test_brand_isolation_list_recent_never_returns_other_brand():
    """Simula o Firestore real filtrando corretamente (o fake so devolve o
    que a query pediu) -- garante que list_recent nao adiciona nenhum
    dossie de fora do resultado da query em cima disso."""
    fake_collection = MagicMock()
    fake_query = MagicMock()

    def _where(field, op, value):
        assert field == "matched_brand" and op == "=="
        docs = [
            {"domain": "itau-fake.com", "matched_brand": "itau"},
        ] if value == "itau" else []
        snapshots = [MagicMock(to_dict=lambda d=d: d) for d in docs]
        result_query = MagicMock()
        result_query.limit.return_value = result_query
        result_query.stream.return_value = snapshots
        return result_query

    fake_collection.where.side_effect = _where

    itau_scope = ba.BrandScopedInvestigations("itau", collection_ref=fake_collection)
    results = itau_scope.list_recent()

    assert all(r["matched_brand"] == "itau" for r in results)
    assert len(results) == 1
