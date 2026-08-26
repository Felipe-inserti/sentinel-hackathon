"""Testes unitarios de `registry.py` -- Firestore sempre mockado (fake
client em memoria, nenhuma chamada real). Cobre publish/get/list/deprecate
e, principalmente, os dois criterios de aceite do Agent Registry:
payload fora do `input_schema` e invocacao de agente nao-ACTIVE devem
falhar com erro claro e auditavel (`AgentInvocationError`)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

import registry
from registry import AgentInvocationError, AgentManifest, AgentNotFoundError, AgentStatus


# --- Fake Firestore (suficiente para .collection().document().get/set/update
# e .collection().where().where().stream(), os unicos metodos usados por
# registry.py) --------------------------------------------------------------


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

    def update(self, data: dict) -> None:
        self._store[self._id].update(data)


class _FakeQuery:
    def __init__(self, docs: list[dict]):
        self._docs = docs

    def where(self, field: str, op: str, value: object) -> "_FakeQuery":
        assert op == "=="
        return _FakeQuery([d for d in self._docs if d.get(field) == value])

    def stream(self) -> list[_FakeDocSnapshot]:
        return [_FakeDocSnapshot(d) for d in self._docs]


class _FakeCollection:
    def __init__(self, store: dict[str, dict]):
        self._store = store

    def document(self, doc_id: str) -> _FakeDocRef:
        return _FakeDocRef(self._store, doc_id)

    def where(self, field: str, op: str, value: object) -> _FakeQuery:
        return _FakeQuery(list(self._store.values())).where(field, op, value)

    def stream(self) -> list[_FakeDocSnapshot]:
        return [_FakeDocSnapshot(d) for d in self._store.values()]


class _FakeFirestoreClient:
    def __init__(self):
        self._collections: dict[str, dict[str, dict]] = {}

    def collection(self, name: str) -> _FakeCollection:
        return _FakeCollection(self._collections.setdefault(name, {}))


@pytest.fixture(autouse=True)
def _fake_db(monkeypatch):
    fake = _FakeFirestoreClient()
    monkeypatch.setattr(registry, "db", fake)
    return fake


# --- Helpers -----------------------------------------------------------

_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"domain": {"type": "string"}},
    "required": ["domain"],
}


def _manifest(
    agent_id: str = "orchestrator",
    version: str = "1.0.0",
    status: AgentStatus = AgentStatus.ACTIVE,
    owner_team: str = "sentinel-investigation",
    input_schema: dict | None = None,
) -> AgentManifest:
    return AgentManifest(
        agent_id=agent_id,
        version=version,
        owner_team=owner_team,
        description="agente de teste",
        input_schema=input_schema or _INPUT_SCHEMA,
        output_schema={"type": "object"},
        tools_allowed=["firestore.read"],
        required_permissions=["roles/datastore.user"],
        sla_seconds=10.0,
        status=status,
        created_at=datetime.now(timezone.utc),
    )


# --- AgentManifest -------------------------------------------------------


def test_manifest_rejects_non_kebab_case_agent_id():
    with pytest.raises(ValidationError):
        _manifest(agent_id="Orchestrator_1")


def test_manifest_rejects_non_semver_version():
    with pytest.raises(ValidationError):
        _manifest(version="1.0")


def test_manifest_doc_id_and_version_tuple():
    manifest = _manifest(agent_id="ct-listener", version="1.2.3")
    assert manifest.doc_id == "ct-listener@1.2.3"
    assert manifest.version_tuple == (1, 2, 3)


# --- publish_agent / get_agent ------------------------------------------


def test_publish_and_get_agent_roundtrip():
    published = _manifest()
    registry.publish_agent(published)

    fetched = registry.get_agent("orchestrator", "1.0.0")
    assert fetched.agent_id == "orchestrator"
    assert fetched.version == "1.0.0"
    assert fetched.status == AgentStatus.ACTIVE


def test_publish_agent_is_idempotent_reseed():
    registry.publish_agent(_manifest())
    registry.publish_agent(_manifest())  # nao deve levantar

    assert registry.get_agent("orchestrator", "1.0.0") is not None


def test_get_agent_missing_raises_not_found():
    with pytest.raises(AgentNotFoundError):
        registry.get_agent("nao-existe", "1.0.0")

    with pytest.raises(AgentNotFoundError):
        registry.get_agent("nao-existe")


def test_get_agent_without_version_picks_highest_active_semver():
    registry.publish_agent(_manifest(version="1.0.0"))
    registry.publish_agent(_manifest(version="1.1.0"))
    registry.publish_agent(_manifest(version="2.0.0", status=AgentStatus.DEPRECATED))

    resolved = registry.get_agent("orchestrator")
    assert resolved.version == "1.1.0"


def test_get_agent_without_version_ignores_disabled_and_deprecated():
    registry.publish_agent(_manifest(version="1.0.0", status=AgentStatus.DISABLED))
    registry.publish_agent(_manifest(version="0.9.0", status=AgentStatus.ACTIVE))

    resolved = registry.get_agent("orchestrator")
    assert resolved.version == "0.9.0"


def test_get_agent_with_explicit_version_returns_any_status():
    registry.publish_agent(_manifest(version="3.0.0", status=AgentStatus.DEPRECATED))

    resolved = registry.get_agent("orchestrator", "3.0.0")
    assert resolved.status == AgentStatus.DEPRECATED


# --- list_agents ---------------------------------------------------------


def test_list_agents_filters_by_status_and_owner_team():
    registry.publish_agent(_manifest(agent_id="ct-listener", owner_team="sentinel-ingestion"))
    registry.publish_agent(
        _manifest(agent_id="takedown-agent", owner_team="sentinel-response", status=AgentStatus.DISABLED)
    )

    all_agents = registry.list_agents()
    assert {m.agent_id for m in all_agents} == {"ct-listener", "takedown-agent"}

    disabled = registry.list_agents(status=AgentStatus.DISABLED)
    assert [m.agent_id for m in disabled] == ["takedown-agent"]

    ingestion = registry.list_agents(owner_team="sentinel-ingestion")
    assert [m.agent_id for m in ingestion] == ["ct-listener"]


# --- deprecate_agent -------------------------------------------------------


def test_deprecate_agent_updates_status():
    registry.publish_agent(_manifest(version="1.0.0"))

    deprecated = registry.deprecate_agent("orchestrator", "1.0.0")
    assert deprecated.status == AgentStatus.DEPRECATED

    # Nao aparece mais como "ultima ACTIVE".
    with pytest.raises(AgentNotFoundError):
        registry.get_agent("orchestrator")


def test_deprecate_agent_missing_raises_not_found():
    with pytest.raises(AgentNotFoundError):
        registry.deprecate_agent("nao-existe", "1.0.0")


# --- invoke_agent (criterios de aceite) -----------------------------------


def test_invoke_agent_success_returns_resolved_manifest():
    registry.publish_agent(_manifest())

    resolved = registry.invoke_agent("orchestrator", {"domain": "nubank-fake.com"})
    assert resolved.version == "1.0.0"


def test_invoke_agent_rejects_deprecated_agent_with_clear_error():
    registry.publish_agent(_manifest(version="1.0.0", status=AgentStatus.DEPRECATED))

    with pytest.raises(AgentInvocationError, match="DEPRECATED"):
        registry.invoke_agent("orchestrator", {"domain": "x.com"}, version="1.0.0")


def test_invoke_agent_rejects_disabled_agent_with_clear_error():
    registry.publish_agent(_manifest(version="1.0.0", status=AgentStatus.DISABLED))

    with pytest.raises(AgentInvocationError, match="DISABLED"):
        registry.invoke_agent("orchestrator", {"domain": "x.com"}, version="1.0.0")


def test_invoke_agent_rejects_payload_outside_input_schema():
    registry.publish_agent(_manifest())

    with pytest.raises(AgentInvocationError):
        registry.invoke_agent("orchestrator", {"nao_e_domain": "x.com"})


def test_invoke_agent_rejects_unknown_agent():
    with pytest.raises(AgentNotFoundError):
        registry.invoke_agent("nao-existe", {"domain": "x.com"})
