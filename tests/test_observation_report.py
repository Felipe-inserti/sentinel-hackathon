"""Testes da Etapa C, item 7 -- `observation_report.py`. Firestore sempre
mockado (via as funcoes `fetch_*`, substituidas diretamente -- o modulo em
si so orquestra `metrics_report.compute_report`/`compute_funnel`, ja
testados por criterios proprios em outros arquivos, mais as secoes NOVAS
desta etapa: top marcas/registrars/ASNs, latencia media, cobertura do
websocket)."""

from __future__ import annotations

from datetime import datetime, timezone

import observation_report as orep


def _totals(**overrides) -> dict:
    base = {
        "certificates_ingested_total": 1000,
        "certificates_discarded_by_prefilter_total": 950,
        "gemma_triage_total": 50,
        "gemma_discarded_total": 30,
        "gemma_escalated_total": 5,
        "gemma_fallback_total": 0,
        "gemma_triage_cost_usd_total": 0.01,
        "llm_invocations_total": 15,
        "cache_hits_total": 5,
        "tokens_consumed_total": 3000,
        "estimated_cost_usd_total": 0.05,
        "malicious_confirmed_total": 4,
        "websocket_disconnects_total": 2,
        "websocket_gap_seconds_total": 40.0,
        "started_at": datetime(2026, 8, 26, tzinfo=timezone.utc),
        "last_updated_at": datetime(2026, 8, 27, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return base


def _investigation(matched_brand=None, registrar=None, asn_org=None, latency=None) -> dict:
    inv: dict = {"matched_brand": matched_brand}
    if registrar or asn_org:
        inv["evidence"] = {}
        if registrar:
            inv["evidence"]["rdap"] = {"registrar": registrar}
        if asn_org:
            inv["evidence"]["hosting"] = {"asn_org": asn_org}
    if latency is not None:
        inv["investigation_latency_seconds"] = latency
    return inv


# --- funcoes de calculo puras ------------------------------------------------


def test_compute_top_brands_counts_and_orders():
    invs = [_investigation("nubank")] * 3 + [_investigation("loggi")] * 1 + [_investigation(None)]
    assert orep.compute_top_brands(invs) == [("nubank", 3), ("loggi", 1)]


def test_compute_top_registrars_and_asns_ignores_missing_evidence():
    invs = [
        _investigation(registrar="GoDaddy", asn_org="Cloudflare"),
        _investigation(registrar="GoDaddy"),
        _investigation(),
    ]
    registrars, asns = orep.compute_top_registrars_and_asns(invs)
    assert registrars == [("GoDaddy", 2)]
    assert asns == [("Cloudflare", 1)]


def test_compute_average_latency_seconds_none_when_no_data():
    assert orep.compute_average_latency_seconds([_investigation(), _investigation()]) is None


def test_compute_average_latency_seconds_computes_mean():
    invs = [_investigation(latency=10.0), _investigation(latency=30.0), _investigation()]
    assert orep.compute_average_latency_seconds(invs) == 20.0


def test_compute_websocket_coverage_averages_gap():
    coverage = orep.compute_websocket_coverage(
        {"websocket_disconnects_total": 2, "websocket_gap_seconds_total": 40.0}
    )
    assert coverage == {"disconnects_total": 2, "gap_seconds_total": 40.0, "avg_gap_seconds": 20.0}


def test_compute_websocket_coverage_zero_disconnects_no_division_error():
    coverage = orep.compute_websocket_coverage({})
    assert coverage["avg_gap_seconds"] == 0.0


# --- build_report / render_text / render_markdown --------------------------


def test_build_report_empty_when_run_has_no_totals(monkeypatch):
    monkeypatch.setattr(orep, "fetch_run_totals", lambda run_id: {})
    report = orep.build_report("obs-inexistente")
    assert report.totals == {}
    assert report.confirmed_malicious == 0


def test_render_text_handles_missing_run_gracefully():
    report = orep.ObservationReport(run_id="obs-inexistente")
    text = orep.render_text(report)
    assert "obs-inexistente" in text
    assert "Nenhum dado encontrado" in text


def test_build_report_wires_all_sections(monkeypatch):
    monkeypatch.setattr(orep, "fetch_run_totals", lambda run_id: _totals())
    monkeypatch.setattr(
        orep,
        "fetch_investigations_since",
        lambda started_at: [
            _investigation("nubank", registrar="GoDaddy", asn_org="Cloudflare", latency=15.0),
            _investigation("nubank", latency=25.0),
            _investigation("loggi"),
        ],
    )
    monkeypatch.setattr(orep, "fetch_checkpoints", lambda run_id: [{"checkpoint_at": "t1"}])

    report = orep.build_report("obs-2026-08-28")

    assert report.confirmed_malicious == 4
    assert report.top_brands == [("nubank", 2), ("loggi", 1)]
    assert report.top_registrars == [("GoDaddy", 1)]
    assert report.top_asns == [("Cloudflare", 1)]
    assert report.avg_latency_seconds == 20.0
    assert report.websocket_coverage["disconnects_total"] == 2
    assert len(report.checkpoints) == 1
    # cascade/funnel reusam metrics_report -- so checamos que vieram
    # preenchidos, a matematica em si e responsabilidade daquele modulo.
    assert report.cascade["ingested"] == 1000
    assert len(report.funnel) == 5


def test_render_text_contains_key_sections(monkeypatch):
    monkeypatch.setattr(orep, "fetch_run_totals", lambda run_id: _totals())
    monkeypatch.setattr(
        orep, "fetch_investigations_since", lambda started_at: [_investigation("nubank", latency=10.0)]
    )
    monkeypatch.setattr(orep, "fetch_checkpoints", lambda run_id: [])

    report = orep.build_report("obs-2026-08-28")
    text = orep.render_text(report)

    assert "obs-2026-08-28" in text
    assert "nubank" in text
    assert "certstream" in text.lower()


def test_render_markdown_contains_tables_and_cost_sections(monkeypatch):
    monkeypatch.setattr(orep, "fetch_run_totals", lambda run_id: _totals())
    monkeypatch.setattr(
        orep,
        "fetch_investigations_since",
        lambda started_at: [_investigation("nubank", registrar="GoDaddy", latency=10.0)],
    )
    monkeypatch.setattr(orep, "fetch_checkpoints", lambda run_id: [{"checkpoint_at": "t1"}])

    report = orep.build_report("obs-2026-08-28")
    md = orep.render_markdown(report)

    assert md.startswith("## Run de observacao `obs-2026-08-28`")
    assert "Custo hipotetico SEM a cascata" in md
    assert "| Marca | Dossies |" in md
    assert "GoDaddy" in md


def test_render_markdown_handles_missing_run_gracefully():
    report = orep.ObservationReport(run_id="obs-inexistente")
    md = orep.render_markdown(report)
    assert "obs-inexistente" in md
    assert "Nenhum dado encontrado" in md
