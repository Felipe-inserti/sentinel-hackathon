"""Testes da integracao orchestrator.py <-> Agent Registry (registry.py).

`registry.invoke_agent` em si ja e coberto exaustivamente em
`tests/test_registry.py` -- aqui o foco e o comportamento do orquestrador
ao redor dela: mensagem invalida/agente nao-ACTIVE deve ser recusada com
nack e SEM chamar `investigate_domain`; mensagem valida deve carimbar
`agent_id`/`agent_version` do manifesto resolvido no dossie persistido."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import plane2_agents.orchestrator as orch
import registry


def _manifest(version: str = "1.0.0", status: registry.AgentStatus = registry.AgentStatus.ACTIVE) -> registry.AgentManifest:
    return registry.AgentManifest(
        agent_id="orchestrator",
        version=version,
        owner_team="sentinel-investigation",
        description="teste",
        input_schema={"type": "object", "properties": {"domain": {"type": "string"}}, "required": ["domain"]},
        output_schema={"type": "object"},
        tools_allowed=[],
        required_permissions=[],
        sla_seconds=10.0,
        status=status,
        created_at=datetime.now(timezone.utc),
    )


def _message(payload: dict) -> MagicMock:
    message = MagicMock()
    message.data = json.dumps(payload).encode("utf-8")
    message.attributes = {}
    return message


@pytest.mark.asyncio
async def test_handle_pubsub_message_rejects_invalid_payload_without_investigating():
    message = _message({"nao_e_domain": "x.com"})
    loop = asyncio.get_running_loop()

    with (
        patch.object(
            orch.registry,
            "invoke_agent",
            side_effect=registry.AgentInvocationError("payload invalido"),
        ),
        patch.object(orch, "investigate_domain", new=AsyncMock()) as fake_investigate,
    ):
        orch._handle_pubsub_message(message, loop)
        await asyncio.sleep(0.2)

    fake_investigate.assert_not_called()
    message.nack.assert_called_once()
    message.ack.assert_not_called()


@pytest.mark.asyncio
async def test_handle_pubsub_message_rejects_deprecated_agent_without_investigating():
    message = _message({"domain": "nubank-fake.com"})
    loop = asyncio.get_running_loop()

    with (
        patch.object(
            orch.registry,
            "invoke_agent",
            side_effect=registry.AgentInvocationError(
                "Agente 'orchestrator@1.0.0' esta com status DEPRECATED -- apenas agentes ACTIVE podem ser invocados"
            ),
        ),
        patch.object(orch, "investigate_domain", new=AsyncMock()) as fake_investigate,
    ):
        orch._handle_pubsub_message(message, loop)
        await asyncio.sleep(0.2)

    fake_investigate.assert_not_called()
    message.nack.assert_called_once()


@pytest.mark.asyncio
async def test_handle_pubsub_message_valid_payload_invokes_with_resolved_manifest():
    manifest = _manifest(version="1.2.0")
    message = _message({"domain": "nubank-fake.com", "matched_brand": "nubank"})
    loop = asyncio.get_running_loop()

    with (
        patch.object(orch.registry, "invoke_agent", return_value=manifest) as fake_invoke,
        patch.object(orch, "investigate_domain", new=AsyncMock(return_value={})) as fake_investigate,
    ):
        orch._handle_pubsub_message(message, loop)
        await asyncio.sleep(0.2)

    fake_invoke.assert_called_once_with("orchestrator", {"domain": "nubank-fake.com", "matched_brand": "nubank"})
    fake_investigate.assert_called_once_with("nubank-fake.com", "nubank", manifest)
    message.ack.assert_called_once()
    message.nack.assert_not_called()


def test_save_investigation_stamps_agent_id_and_version(monkeypatch):
    fake_db = MagicMock()
    monkeypatch.setattr(orch, "db", fake_db)

    manifest = _manifest(version="2.3.1")
    result = orch.AnalysisResult(classification="SAFE", confidence=0.9, reasoning="ok")
    usage = orch.LLMUsage(model_id="gemini-3.6-flash", input_tokens=10, output_tokens=5, latency_ms=100.0)
    sanitized = orch.SanitizationResult(clean_text="ok", injection_patterns_found=[], pii_redacted={})

    orch._save_investigation("dominio-teste.com", "nubank", result, usage, sanitized, 0.0001, manifest)

    set_call = fake_db.collection.return_value.document.return_value.set
    set_call.assert_called_once()
    saved = set_call.call_args[0][0]
    assert saved["agent_id"] == "orchestrator"
    assert saved["agent_version"] == "2.3.1"
