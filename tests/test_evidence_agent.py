"""Testes de `evidence_agent.py` -- rede/Firestore/GCS/Playwright sempre
mockados (nenhuma chamada real). Foco:

  1. Falha graciosa: cada secao de coleta que falha vira `CollectionError`
     isolado, nunca derruba as outras (`test_collect_evidence_*`).
  2. Chain of custody: `manifest_root_hash` e reproduzivel e sensivel a
     conteudo (`test_compute_root_hash_*`).
  3. O filtro `classification == "MALICIOUS"` e o carimbo de
     `agent_id`/`agent_version` ao persistir -- mesmo criterio de aceite
     usado em `tests/test_orchestrator_registry.py`."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import dns.resolver
import pytest

import evidence_agent as ea
import registry


def _manifest(
    agent_id: str = "evidence-collector",
    version: str = "2.0.0",
    status: registry.AgentStatus = registry.AgentStatus.ACTIVE,
) -> registry.AgentManifest:
    return registry.AgentManifest(
        agent_id=agent_id,
        version=version,
        owner_team="sentinel-evidence",
        description="teste",
        input_schema={"type": "object", "properties": {"domain": {"type": "string"}}, "required": ["domain"]},
        output_schema={"type": "object"},
        tools_allowed=[],
        required_permissions=[],
        sla_seconds=30.0,
        status=status,
        created_at=datetime.now(timezone.utc),
    )


def _message(payload: dict) -> MagicMock:
    message = MagicMock()
    message.data = json.dumps(payload).encode("utf-8")
    message.attributes = {}
    return message


# --- _handle_pubsub_message --------------------------------------------------


@pytest.mark.asyncio
async def test_handle_pubsub_message_rejects_invalid_payload_without_collecting():
    message = _message({"nao_e_domain": "x.com"})
    loop = asyncio.get_running_loop()

    with (
        patch.object(ea.registry, "invoke_agent", side_effect=registry.AgentInvocationError("payload invalido")),
        patch.object(ea, "collect_evidence", new=AsyncMock()) as fake_collect,
    ):
        ea._handle_pubsub_message(message, loop)
        await asyncio.sleep(0.2)

    fake_collect.assert_not_called()
    message.nack.assert_called_once()
    message.ack.assert_not_called()


@pytest.mark.asyncio
async def test_handle_pubsub_message_ignores_safe_classification():
    manifest = _manifest()
    message = _message(
        {"domain": "banco-legitimo.com", "classification": "SAFE", "confidence": 0.9, "cache_hit": False}
    )
    loop = asyncio.get_running_loop()

    with (
        patch.object(ea.registry, "invoke_agent", return_value=manifest),
        patch.object(ea, "collect_evidence", new=AsyncMock()) as fake_collect,
    ):
        ea._handle_pubsub_message(message, loop)
        await asyncio.sleep(0.2)

    fake_collect.assert_not_called()
    message.ack.assert_called_once()
    message.nack.assert_not_called()


@pytest.mark.asyncio
async def test_handle_pubsub_message_malicious_collects_and_stamps_agent(monkeypatch):
    manifest = _manifest(version="2.0.0")
    message = _message(
        {"domain": "nubank-fake.com", "classification": "MALICIOUS", "confidence": 0.95, "cache_hit": False}
    )
    loop = asyncio.get_running_loop()

    bundle = ea.EvidenceBundle(
        domain="nubank-fake.com", collected_at=datetime.now(timezone.utc), manifest_root_hash="abc"
    )
    fake_update = MagicMock()
    monkeypatch.setattr(ea, "_update_dossier_with_evidence", fake_update)

    with (
        patch.object(ea.registry, "invoke_agent", return_value=manifest),
        patch.object(ea, "collect_evidence", new=AsyncMock(return_value=bundle)) as fake_collect,
    ):
        ea._handle_pubsub_message(message, loop)
        await asyncio.sleep(0.2)

    fake_collect.assert_called_once_with("nubank-fake.com")
    fake_update.assert_called_once_with("nubank-fake.com", bundle, manifest)
    message.ack.assert_called_once()
    message.nack.assert_not_called()


@pytest.mark.asyncio
async def test_handle_pubsub_message_nacks_on_unexpected_collection_error(monkeypatch):
    manifest = _manifest()
    message = _message(
        {"domain": "nubank-fake.com", "classification": "MALICIOUS", "confidence": 0.95, "cache_hit": False}
    )
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(ea, "_update_dossier_with_evidence", MagicMock())

    with (
        patch.object(ea.registry, "invoke_agent", return_value=manifest),
        patch.object(ea, "collect_evidence", new=AsyncMock(side_effect=RuntimeError("bug inesperado"))),
    ):
        ea._handle_pubsub_message(message, loop)
        await asyncio.sleep(0.2)

    message.nack.assert_called_once()
    message.ack.assert_not_called()


# --- _update_dossier_with_evidence -------------------------------------------


def test_update_dossier_with_evidence_merges_and_stamps_agent(monkeypatch):
    fake_db = MagicMock()
    monkeypatch.setattr(ea, "db", fake_db)

    manifest = _manifest(version="2.1.0")
    bundle = ea.EvidenceBundle(domain="x.com", collected_at=datetime.now(timezone.utc), manifest_root_hash="deadbeef")

    ea._update_dossier_with_evidence("x.com", bundle, manifest)

    set_call = fake_db.collection.return_value.document.return_value.set
    set_call.assert_called_once()
    args, kwargs = set_call.call_args
    saved = args[0]
    assert saved["status"] == ea.DOSSIER_STATUS_PENDING_HUMAN_REVIEW
    assert saved["evidence_agent_id"] == "evidence-collector"
    assert saved["evidence_agent_version"] == "2.1.0"
    assert saved["evidence"]["domain"] == "x.com"
    assert kwargs["merge"] is True


# --- chain of custody: manifest_root_hash ------------------------------------


def test_compute_root_hash_is_sha256_hex_and_sensitive_to_content():
    bundle_a = ea.EvidenceBundle(domain="a.com", collected_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    bundle_b = bundle_a.model_copy(update={"domain": "b.com"})

    hash_a = ea._compute_root_hash(bundle_a)
    hash_b = ea._compute_root_hash(bundle_b)

    assert len(hash_a) == 64
    assert hash_a != hash_b


def test_compute_root_hash_ignores_current_value_of_the_hash_field_itself():
    bundle_a = ea.EvidenceBundle(domain="a.com", collected_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    bundle_b = bundle_a.model_copy(update={"manifest_root_hash": "valor-de-uma-rodada-anterior"})

    assert ea._compute_root_hash(bundle_a) == ea._compute_root_hash(bundle_b)


# --- fingerprint de infraestrutura -------------------------------------------


def test_normalize_dom_structure_ignores_text_content():
    html_a = "<html><body><div>Ola vitima 1</div></body></html>"
    html_b = "<html><body><div>Ola vitima 2</div></body></html>"
    assert ea._normalize_dom_structure(html_a) == ea._normalize_dom_structure(html_b)


def test_normalize_dom_structure_differs_for_different_structure():
    html_a = "<html><body><div>x</div></body></html>"
    html_b = "<html><body><form>x</form></body></html>"
    assert ea._normalize_dom_structure(html_a) != ea._normalize_dom_structure(html_b)


def test_compute_fingerprint_stable_for_same_inputs():
    hosting = ea.HostingInfo(ip_address="1.2.3.4", asn=123, asn_org="Test ASN")
    fp1 = ea._compute_fingerprint("<html><body></body></html>", hosting, None, None)
    fp2 = ea._compute_fingerprint("<html><body></body></html>", hosting, None, None)
    assert fp1.fingerprint_hash == fp2.fingerprint_hash
    assert fp1.fingerprint_hash is not None


# --- falha graciosa por secao ------------------------------------------------


def test_collect_dns_all_types_failing_returns_collection_error(monkeypatch):
    class _FakeResolver:
        timeout = None
        lifetime = None

        def resolve(self, name, qtype):
            raise dns.resolver.NXDOMAIN()

    monkeypatch.setattr(ea.dns.resolver, "Resolver", _FakeResolver)

    records, err = ea._collect_dns("dominio-inexistente.test")

    assert records.a == []
    assert err is not None
    assert err.step == "dns"


def test_collect_dns_partial_success_is_not_an_error(monkeypatch):
    class _FakeAnswerRecord:
        def __init__(self, text):
            self._text = text

        def to_text(self):
            return self._text

    class _FakeResolver:
        timeout = None
        lifetime = None

        def resolve(self, name, qtype):
            if qtype == "A":
                return [_FakeAnswerRecord("1.2.3.4")]
            raise dns.resolver.NoAnswer()

    monkeypatch.setattr(ea.dns.resolver, "Resolver", _FakeResolver)

    records, err = ea._collect_dns("exemplo.test")

    assert records.a == ["1.2.3.4"]
    assert records.mx == []
    assert err is None


def test_fetch_http_snapshot_network_failure_returns_collection_error(monkeypatch):
    def _raise(*args, **kwargs):
        raise ea.requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(ea.requests, "get", _raise)

    snapshot, html_text, err = ea._fetch_http_snapshot("https://site-fora-do-ar.test")

    assert snapshot is None
    assert html_text is None
    assert err is not None
    assert err.step == "http_snapshot"


def test_collect_tls_certificate_connection_failure_returns_collection_error(monkeypatch):
    def _raise(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(ea.socket, "create_connection", _raise)

    cert, err = ea._collect_tls_certificate("site-fora-do-ar.test")

    assert cert is None
    assert err is not None
    assert err.step == "tls_certificate"


def test_collect_rdap_domain_without_bootstrap_match_returns_collection_error(monkeypatch):
    monkeypatch.setattr(ea, "_rdap_domain_base_url", lambda domain: None)

    rdap, err = ea._collect_rdap_domain("x.tld-desconhecido")

    assert rdap is None
    assert err is not None
    assert err.step == "rdap"


# --- collect_evidence: orquestracao completa, bundle parcial ----------------


@pytest.mark.asyncio
async def test_collect_evidence_produces_partial_bundle_when_some_sections_fail(monkeypatch):
    monkeypatch.setattr(
        ea,
        "_capture_screenshot_and_form_signal",
        AsyncMock(return_value=(None, ea.FormFieldSignal(), ea.CollectionError(step="screenshot", error="timeout"))),
    )
    monkeypatch.setattr(
        ea, "_fetch_http_snapshot", lambda url: (ea.HttpResponseSnapshot(status_code=200), "<html></html>", None)
    )
    monkeypatch.setattr(ea, "_collect_dns", lambda domain: (ea.DnsRecords(a=["1.2.3.4"]), None))
    monkeypatch.setattr(
        ea, "_collect_hosting", lambda ip: (ea.HostingInfo(ip_address=ip, asn=1, asn_org="Teste ASN"), None)
    )
    monkeypatch.setattr(
        ea, "_collect_tls_certificate", lambda domain: (None, ea.CollectionError(step="tls_certificate", error="sem cert"))
    )
    monkeypatch.setattr(
        ea, "_collect_rdap_domain", lambda domain: (None, ea.CollectionError(step="rdap", error="sem servidor RDAP"))
    )
    monkeypatch.setattr(ea.storage_client, "bucket", lambda name: MagicMock())
    monkeypatch.setattr(
        ea,
        "_upload_artifact",
        lambda bucket, domain, filename, data, content_type: ea.ArtifactRef(
            gcs_uri=f"gs://fake/{filename}", sha256="deadbeef", content_type=content_type, size_bytes=len(data)
        ),
    )
    monkeypatch.setattr(ea.telemetry, "flush_metrics_to_firestore", lambda deltas: None)
    monkeypatch.setattr(ea.telemetry, "increment_counter", lambda *a, **k: None)

    bundle = await ea.collect_evidence("phish.test")

    assert bundle.is_partial is True
    assert bundle.screenshot is None
    assert bundle.html_snapshot is not None
    assert bundle.hosting is not None and bundle.hosting.asn == 1
    assert len(bundle.manifest_root_hash) == 64
    assert {e.step for e in bundle.collection_errors} == {"screenshot", "tls_certificate", "rdap"}


@pytest.mark.asyncio
async def test_collect_evidence_full_success_is_not_partial(monkeypatch):
    monkeypatch.setattr(
        ea, "_capture_screenshot_and_form_signal", AsyncMock(return_value=(b"fake-png-bytes", ea.FormFieldSignal(), None))
    )
    monkeypatch.setattr(
        ea, "_fetch_http_snapshot", lambda url: (ea.HttpResponseSnapshot(status_code=200), "<html></html>", None)
    )
    monkeypatch.setattr(ea, "_collect_dns", lambda domain: (ea.DnsRecords(a=["1.2.3.4"]), None))
    monkeypatch.setattr(ea, "_collect_hosting", lambda ip: (ea.HostingInfo(ip_address=ip, asn=1), None))
    monkeypatch.setattr(
        ea, "_collect_tls_certificate", lambda domain: (ea.TlsCertificateInfo(issuer="CA de teste"), None)
    )
    monkeypatch.setattr(ea, "_collect_rdap_domain", lambda domain: (ea.RdapInfo(registrar="Registrar de teste"), None))
    monkeypatch.setattr(ea.storage_client, "bucket", lambda name: MagicMock())
    monkeypatch.setattr(
        ea,
        "_upload_artifact",
        lambda bucket, domain, filename, data, content_type: ea.ArtifactRef(
            gcs_uri=f"gs://fake/{filename}", sha256="deadbeef", content_type=content_type, size_bytes=len(data)
        ),
    )
    monkeypatch.setattr(ea.telemetry, "flush_metrics_to_firestore", lambda deltas: None)
    monkeypatch.setattr(ea.telemetry, "increment_counter", lambda *a, **k: None)

    bundle = await ea.collect_evidence("phish-completo.test")

    assert bundle.is_partial is False
    assert bundle.collection_errors == []
    assert bundle.screenshot is not None
    assert bundle.html_snapshot is not None


# --- output_schema publicavel -------------------------------------------------


def test_evidence_bundle_schema_has_expected_top_level_fields():
    schema = ea.EvidenceBundle.model_json_schema()
    expected_fields = {
        "domain",
        "collected_at",
        "screenshot",
        "html_snapshot",
        "http_response",
        "dns_records",
        "hosting",
        "tls_certificate",
        "rdap",
        "infrastructure_fingerprint",
        "pii_redacted",
        "form_fields_detected",
        "collection_errors",
        "is_partial",
        "manifest_root_hash",
    }
    assert expected_fields <= schema["properties"].keys()
