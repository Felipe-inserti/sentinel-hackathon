"""Testes de `agent_gateway.py` -- Firestore/Pub/Sub/verificacao de ID
token sempre fakes, nenhuma chamada de rede real (mesmo principio de
`tests/test_registry.py`/`tests/test_takedown_agent.py`). Cobre as 7
etapas do pipeline, NESTA ORDEM, e os tres criterios de aceite explicitos
do requisito Agent Gateway:

  1. Toda rejeicao devolve `GatewayRejection` com a etapa correta
     (autenticacao/resolucao/schema/rate limit/autorizacao/roteamento).
  2. Toda chamada -- sucesso OU rejeicao em qualquer etapa -- grava um
     registro em `agent_gateway_audit_log`.
  3. GET /agents lista o registry; POST /invoke/{agent_id} devolve erro
     estruturado (nunca 500 generico) em qualquer rejeicao."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import agent_gateway as gw
import registry

_CALLER = "caller@test-project.iam.gserviceaccount.com"
_DOMAIN_SCHEMA = {
    "type": "object",
    "properties": {"domain": {"type": "string"}},
    "required": ["domain"],
}


# --- Fakes / helpers ---------------------------------------------------


def _manifest(
    agent_id: str = "orchestrator",
    version: str = "1.0.0",
    status: registry.AgentStatus = registry.AgentStatus.ACTIVE,
    input_schema: dict | None = None,
) -> registry.AgentManifest:
    return registry.AgentManifest(
        agent_id=agent_id,
        version=version,
        owner_team="sentinel-test",
        description="agente de teste",
        input_schema=input_schema or _DOMAIN_SCHEMA,
        output_schema={"type": "object"},
        tools_allowed=[],
        required_permissions=[],
        sla_seconds=10.0,
        status=status,
        created_at=datetime.now(timezone.utc),
    )


class _FakeSnapshot:
    def __init__(self, data: dict | None) -> None:
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict | None:
        return self._data


def _fake_db(rate_limit_count: int | None = 0) -> MagicMock:
    """Firestore fake: `.collection(...).document(...).get()` devolve um
    snapshot com `count=rate_limit_count` (None = documento inexistente);
    `.transaction()` devolve um MagicMock inerte; `.collection(...).add()`
    fica disponivel para asserts de auditoria."""
    fake_db = MagicMock()
    snapshot = _FakeSnapshot({"count": rate_limit_count} if rate_limit_count is not None else None)
    fake_db.collection.return_value.document.return_value.get.return_value = snapshot
    fake_db.transaction.return_value = MagicMock()
    return fake_db


def _fake_publisher(message_id: str = "msg-1", publish_error: Exception | None = None) -> MagicMock:
    fake_publisher = MagicMock()
    fake_publisher.topic_path.side_effect = lambda project, topic: f"projects/{project}/topics/{topic}"
    if publish_error is not None:
        fake_publisher.publish.return_value.result.side_effect = publish_error
    else:
        fake_publisher.publish.return_value.result.return_value = message_id
    return fake_publisher


def _gateway(
    *,
    verify_token=lambda token: {"email": _CALLER},
    db: MagicMock | None = None,
    publisher: MagicMock | None = None,
    authorization_policy=None,
    routing_table=None,
) -> gw.AgentGateway:
    return gw.AgentGateway(
        verify_token=verify_token,
        db=db if db is not None else _fake_db(),
        publisher=publisher if publisher is not None else _fake_publisher(),
        now_fn=lambda: datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc),
        authorization_policy=authorization_policy,
        routing_table=routing_table,
    )


@pytest.fixture(autouse=True)
def _no_op_transactional(monkeypatch):
    # Mesmo truque de tests/test_takedown_agent.py: substitui o decorator
    # `@firestore.transactional` por identidade, para nao precisar de uma
    # `firestore.Transaction` real dentro do MagicMock.
    monkeypatch.setattr(gw.firestore, "transactional", lambda fn: fn)


# --- Tabelas de politica/roteamento (defaults do modulo) ----------------


def test_authorization_policy_blocks_takedown_agent_for_everyone():
    # frozenset() vazio -- NENHUM chamador, nem dashboard-sa, esta na lista
    # (decisao arquitetural: ver comentario extenso em agent_gateway.py e
    # infra/README.md -- o gateway nunca ganha publish em 'takedown-approved').
    assert gw.AUTHORIZATION_POLICY["takedown-agent"] == frozenset()


def test_routing_table_does_not_include_ct_listener_or_takedown_agent():
    assert "ct-listener" not in gw.AGENT_ROUTING_TOPIC
    assert "takedown-agent" not in gw.AGENT_ROUTING_TOPIC


def test_routing_table_maps_orchestrator_and_evidence():
    from config import settings

    assert gw.AGENT_ROUTING_TOPIC["orchestrator"] == settings.suspicious_topic_id
    assert gw.AGENT_ROUTING_TOPIC["evidence-collector"] == settings.completed_topic_id


# --- Etapa 1: autenticacao ------------------------------------------------


def test_missing_authorization_header_rejected_at_authentication():
    gateway = _gateway()

    with pytest.raises(gw.GatewayRejection) as exc_info:
        gateway.handle_invocation("orchestrator", b"{}", authorization_header=None)

    assert exc_info.value.stage == "authentication"


def test_header_without_bearer_prefix_rejected_at_authentication():
    gateway = _gateway()

    with pytest.raises(gw.GatewayRejection) as exc_info:
        gateway.handle_invocation("orchestrator", b"{}", authorization_header="Token abc")

    assert exc_info.value.stage == "authentication"


def test_invalid_token_rejected_at_authentication():
    def _verify_raises(token: str) -> dict:
        raise ValueError("assinatura invalida")

    gateway = _gateway(verify_token=_verify_raises)

    with pytest.raises(gw.GatewayRejection) as exc_info:
        gateway.handle_invocation("orchestrator", b"{}", authorization_header="Bearer bad-token")

    assert exc_info.value.stage == "authentication"


def test_authentication_failure_is_audited_with_no_caller_identity():
    fake_db = _fake_db()
    gateway = _gateway(db=fake_db)

    with pytest.raises(gw.GatewayRejection):
        gateway.handle_invocation("orchestrator", b"{}", authorization_header=None)

    fake_db.collection.return_value.add.assert_called_once()
    (record,), _ = fake_db.collection.return_value.add.call_args
    assert record["stage_reached"] == "authentication"
    assert record["outcome"] == "REJECTED"
    assert record["caller_identity"] is None


# --- Etapa 2: resolucao no registry ---------------------------------------


def test_unknown_agent_rejected_at_resolution():
    gateway = _gateway()

    with patch.object(
        gw.registry, "get_agent", side_effect=registry.AgentNotFoundError("nao existe")
    ):
        with pytest.raises(gw.GatewayRejection) as exc_info:
            gateway.handle_invocation("nao-existe", b"{}", authorization_header="Bearer x")

    assert exc_info.value.stage == "resolution"


def test_deprecated_agent_explicit_version_rejected_at_resolution():
    gateway = _gateway()

    with patch.object(
        gw.registry, "get_agent", return_value=_manifest(status=registry.AgentStatus.DEPRECATED)
    ):
        with pytest.raises(gw.GatewayRejection) as exc_info:
            gateway.handle_invocation(
                "orchestrator", b'{"domain":"x.com"}', authorization_header="Bearer x", version="1.0.0"
            )

    assert exc_info.value.stage == "resolution"
    assert exc_info.value.error == "agent_not_active"


# --- Etapa 3: validacao de schema ------------------------------------------


def test_payload_outside_schema_rejected_at_schema_validation():
    gateway = _gateway()

    with patch.object(gw.registry, "get_agent", return_value=_manifest()):
        with pytest.raises(gw.GatewayRejection) as exc_info:
            gateway.handle_invocation(
                "orchestrator", b'{"nao_e_domain": "x"}', authorization_header="Bearer x"
            )

    assert exc_info.value.stage == "schema_validation"
    assert exc_info.value.error == "payload_invalid"


def test_malformed_json_body_rejected_after_resolution_not_before():
    gateway = _gateway()

    with patch.object(gw.registry, "get_agent", return_value=_manifest()) as fake_get_agent:
        with pytest.raises(gw.GatewayRejection) as exc_info:
            gateway.handle_invocation(
                "orchestrator", b"{not valid json", authorization_header="Bearer x"
            )

    # Resolucao (etapa 2) roda ANTES do corpo malformado ser detectado --
    # prova de que a ordem do pipeline e respeitada mesmo num corpo invalido.
    fake_get_agent.assert_called_once()
    assert exc_info.value.stage == "schema_validation"
    assert exc_info.value.error == "malformed_json_body"


# --- Etapa 4: rate limit ----------------------------------------------------


def test_rate_limit_exceeded_rejected_without_incrementing():
    from config import settings

    fake_db = _fake_db(rate_limit_count=settings.agent_gateway_rate_limit_per_minute)
    gateway = _gateway(db=fake_db)

    with patch.object(gw.registry, "get_agent", return_value=_manifest()):
        with pytest.raises(gw.GatewayRejection) as exc_info:
            gateway.handle_invocation(
                "orchestrator", b'{"domain":"x.com"}', authorization_header="Bearer x"
            )

    assert exc_info.value.stage == "rate_limit"
    fake_db.transaction.return_value.set.assert_not_called()


def test_rate_limit_within_bounds_increments_and_proceeds():
    fake_db = _fake_db(rate_limit_count=1)
    gateway = _gateway(db=fake_db)

    with patch.object(gw.registry, "get_agent", return_value=_manifest()):
        gateway.handle_invocation("orchestrator", b'{"domain":"x.com"}', authorization_header="Bearer x")

    fake_db.transaction.return_value.set.assert_called_once()
    args, kwargs = fake_db.transaction.return_value.set.call_args
    assert args[1]["count"] == 2
    assert kwargs["merge"] is True


# --- Etapa 5: politica de autorizacao ---------------------------------------


def test_takedown_agent_rejects_arbitrary_caller_with_dedicated_message():
    gateway = _gateway(verify_token=lambda token: {"email": "someone-else@test-project.iam.gserviceaccount.com"})

    with patch.object(
        gw.registry, "get_agent", return_value=_manifest(agent_id="takedown-agent")
    ):
        with pytest.raises(gw.GatewayRejection) as exc_info:
            gateway.handle_invocation(
                "takedown-agent", b'{"domain":"x.com"}', authorization_header="Bearer x"
            )

    assert exc_info.value.stage == "authorization"
    assert exc_info.value.error == "human_approval_required_via_dashboard"


def test_takedown_agent_rejects_even_dashboard_sa_itself():
    # Decisao arquitetural deliberada (nao uma lacuna): NENHUM chamador
    # invoca takedown-agent via gateway, nem a propria identidade
    # dashboard-sa -- o unico caminho real continua sendo o fluxo humano
    # do dashboard publicando direto em 'takedown-approved'. Ver
    # AUTHORIZATION_POLICY em agent_gateway.py e infra/README.md.
    dashboard_email = "dashboard-sa@test-project.iam.gserviceaccount.com"
    gateway = _gateway(verify_token=lambda token: {"email": dashboard_email})

    with patch.object(
        gw.registry, "get_agent", return_value=_manifest(agent_id="takedown-agent")
    ):
        with pytest.raises(gw.GatewayRejection) as exc_info:
            gateway.handle_invocation(
                "takedown-agent", b'{"domain":"x.com"}', authorization_header="Bearer x"
            )

    assert exc_info.value.stage == "authorization"
    assert exc_info.value.error == "human_approval_required_via_dashboard"


def test_takedown_agent_never_reaches_routing_stage():
    """Confirma que a rejeicao acontece na etapa 5 (autorizacao), nao na 6
    (roteamento) -- o publisher fake nunca deveria ser chamado."""
    fake_publisher = _fake_publisher()
    gateway = _gateway(
        verify_token=lambda token: {"email": "dashboard-sa@test-project.iam.gserviceaccount.com"},
        publisher=fake_publisher,
    )

    with patch.object(
        gw.registry, "get_agent", return_value=_manifest(agent_id="takedown-agent")
    ):
        with pytest.raises(gw.GatewayRejection):
            gateway.handle_invocation(
                "takedown-agent", b'{"domain":"x.com"}', authorization_header="Bearer x"
            )

    fake_publisher.publish.assert_not_called()


# --- Etapa 6: roteamento -----------------------------------------------


def test_ct_listener_is_authorized_but_rejected_as_not_routable():
    gateway = _gateway()

    with patch.object(
        gw.registry,
        "get_agent",
        return_value=_manifest(agent_id="ct-listener", input_schema={"type": "object"}),
    ):
        with pytest.raises(gw.GatewayRejection) as exc_info:
            gateway.handle_invocation("ct-listener", b"{}", authorization_header="Bearer x")

    assert exc_info.value.stage == "routing"
    assert exc_info.value.error == "not_routable"


def test_publish_failure_wrapped_as_routing_rejection():
    gateway = _gateway(publisher=_fake_publisher(publish_error=RuntimeError("pubsub indisponivel")))

    with patch.object(gw.registry, "get_agent", return_value=_manifest()):
        with pytest.raises(gw.GatewayRejection) as exc_info:
            gateway.handle_invocation(
                "orchestrator", b'{"domain":"x.com"}', authorization_header="Bearer x"
            )

    assert exc_info.value.stage == "routing"
    assert exc_info.value.error == "publish_failed"


# --- Etapa 7: auditoria + sucesso fim a fim --------------------------------


def test_successful_invocation_returns_topic_and_message_id_and_audits_allowed():
    from config import settings

    fake_db = _fake_db()
    gateway = _gateway(db=fake_db)

    with patch.object(gw.registry, "get_agent", return_value=_manifest()):
        result = gateway.handle_invocation(
            "orchestrator", b'{"domain":"nubank-fake.com"}', authorization_header="Bearer x"
        )

    assert result.agent_id == "orchestrator"
    assert result.agent_version == "1.0.0"
    assert result.caller_identity == _CALLER
    assert result.routed_to_topic == settings.suspicious_topic_id
    assert result.message_id == "msg-1"

    fake_db.collection.return_value.add.assert_called_once()
    (record,), _ = fake_db.collection.return_value.add.call_args
    assert record["outcome"] == "ALLOWED"
    assert record["stage_reached"] == "routing"
    assert record["caller_identity"] == _CALLER


def test_audit_write_failure_never_masks_the_original_rejection(monkeypatch):
    fake_db = _fake_db()
    fake_db.collection.return_value.add.side_effect = RuntimeError("firestore indisponivel")
    gateway = _gateway(db=fake_db)

    with pytest.raises(gw.GatewayRejection) as exc_info:
        gateway.handle_invocation("orchestrator", b"{}", authorization_header=None)

    # A excecao original (authentication) continua a que sobe, mesmo com o
    # log de auditoria falhando internamente.
    assert exc_info.value.stage == "authentication"


# --- App FastAPI ---------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    gateway = _gateway()
    app = gw.create_app(gateway=gateway)
    return TestClient(app)


def test_readyz_does_not_require_auth(client: TestClient):
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_get_agents_requires_authentication(client: TestClient):
    response = client.get("/agents")

    assert response.status_code == 401
    body = response.json()
    assert body["stage"] == "authentication"


def test_get_agents_lists_registry_when_authenticated(client: TestClient):
    with patch.object(gw.registry, "list_agents", return_value=[_manifest(), _manifest(agent_id="ct-listener")]):
        response = client.get("/agents", headers={"Authorization": "Bearer x"})

    assert response.status_code == 200
    body = response.json()
    assert {item["agent_id"] for item in body} == {"orchestrator", "ct-listener"}
    routable = {item["agent_id"]: item["routable"] for item in body}
    assert routable["orchestrator"] is True
    assert routable["ct-listener"] is False


def test_invoke_endpoint_returns_structured_error_body(client: TestClient):
    response = client.post("/invoke/orchestrator", json={"domain": "x.com"})

    assert response.status_code == 401
    body = response.json()
    assert body["stage"] == "authentication"
    assert body["agent_id"] == "orchestrator"
    assert "timestamp" in body


def test_invoke_endpoint_success(client: TestClient):
    from config import settings

    with patch.object(gw.registry, "get_agent", return_value=_manifest()):
        response = client.post(
            "/invoke/orchestrator",
            json={"domain": "nubank-fake.com"},
            headers={"Authorization": "Bearer x"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["routed_to_topic"] == settings.suspicious_topic_id
    assert body["message_id"] == "msg-1"


def test_invoke_endpoint_schema_rejection_returns_422(client: TestClient):
    with patch.object(gw.registry, "get_agent", return_value=_manifest()):
        response = client.post(
            "/invoke/orchestrator",
            json={"nao_e_domain": "x"},
            headers={"Authorization": "Bearer x"},
        )

    assert response.status_code == 422
    assert response.json()["stage"] == "schema_validation"
