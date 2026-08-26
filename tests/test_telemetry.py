"""Teste de regressao para o bug de regiao do OTel (Sprint 8, Parte B):
reproduzido ao vivo contra o backend real da Telemetry API nesta sessao --
sem um `cloud.region` valido no Resource, o ingest de METRICAS (traces
exportam OK sem isso) rejeita com "write for resource failed: Unrecognized
region or location". `OTEL_ENABLED=false` -- nenhuma chamada de rede real
aqui, so a construcao do `Resource` em memoria."""

from __future__ import annotations

import telemetry
from config import settings


def test_setup_resource_includes_valid_cloud_region(monkeypatch):
    monkeypatch.setattr(settings, "otel_enabled", False)
    telemetry.setup("test-service")

    # `get_tracer()` expoe o tracer, mas o Resource fica no provider --
    # inspecionamos via o proprio TracerProvider global que setup() configurou.
    from opentelemetry import trace as trace_api

    resource = trace_api.get_tracer_provider().resource
    attributes = resource.attributes

    assert attributes.get("cloud.region") == settings.otel_region
    assert attributes.get("cloud.region") != settings.gcp_location
    assert attributes.get("gcp.project_id") == settings.gcp_project_id
    assert attributes.get("cloud.account.id") == settings.gcp_project_id


def test_otel_region_is_not_gcp_location():
    # `gcp_location` ("global", endpoint do Vertex AI) NUNCA e um valor
    # valido pra `otel_region` -- a causa raiz do bug original era
    # exatamente reaproveitar essa variavel. Guarda contra reintroduzir a
    # mesma confusao no default.
    assert settings.otel_region != settings.gcp_location
    assert settings.otel_region == "us-central1"
