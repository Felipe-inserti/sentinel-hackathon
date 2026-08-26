"""Testes de `sync_brand_memory.py` -- brand_agent/brand_memory sempre
mockados. Foco: so decisoes terminais (REJECTED/TAKEDOWN_APPROVED) sao
sincronizadas, cada uma vai para a funcao de gravacao correta, e a leitura
usa exclusivamente `BrandScopedInvestigations` (isolamento por marca)."""

from __future__ import annotations

from unittest.mock import MagicMock

import sync_brand_memory as sbm


def test_sync_brand_uses_brand_scoped_investigations(monkeypatch):
    fake_scope = MagicMock()
    fake_scope.list_by_status.return_value = []
    monkeypatch.setattr(sbm.brand_agent, "BrandScopedInvestigations", MagicMock(return_value=fake_scope))

    sbm.sync_brand("nubank", limit=100)

    sbm.brand_agent.BrandScopedInvestigations.assert_called_once_with("nubank")
    fake_scope.list_by_status.assert_called_once_with(["REJECTED", "TAKEDOWN_APPROVED"], limit=100)


def test_sync_brand_routes_rejected_and_approved_to_correct_recorder(monkeypatch):
    fake_scope = MagicMock()
    fake_scope.list_by_status.return_value = [
        {"domain": "rejeitado.com", "status": "REJECTED"},
        {"domain": "aprovado.com", "status": "TAKEDOWN_APPROVED"},
    ]
    monkeypatch.setattr(sbm.brand_agent, "BrandScopedInvestigations", MagicMock(return_value=fake_scope))

    fake_record_rejection = MagicMock()
    fake_record_approval = MagicMock()
    monkeypatch.setattr(sbm.brand_memory, "record_rejection", fake_record_rejection)
    monkeypatch.setattr(sbm.brand_memory, "record_approval", fake_record_approval)

    rejected_count, approved_count = sbm.sync_brand("nubank")

    assert (rejected_count, approved_count) == (1, 1)
    fake_record_rejection.assert_called_once_with(
        brand_id="nubank", domain="rejeitado.com", investigation={"domain": "rejeitado.com", "status": "REJECTED"}
    )
    fake_record_approval.assert_called_once_with(
        brand_id="nubank",
        domain="aprovado.com",
        investigation={"domain": "aprovado.com", "status": "TAKEDOWN_APPROVED"},
    )


def test_sync_brand_skips_documents_without_domain(monkeypatch):
    fake_scope = MagicMock()
    fake_scope.list_by_status.return_value = [{"status": "REJECTED"}]
    monkeypatch.setattr(sbm.brand_agent, "BrandScopedInvestigations", MagicMock(return_value=fake_scope))
    fake_record_rejection = MagicMock()
    monkeypatch.setattr(sbm.brand_memory, "record_rejection", fake_record_rejection)

    rejected_count, approved_count = sbm.sync_brand("nubank")

    assert (rejected_count, approved_count) == (0, 0)
    fake_record_rejection.assert_not_called()


def test_sync_brand_continues_after_one_failure(monkeypatch):
    """Uma falha ao sincronizar UM dominio nunca derruba o resto do lote."""
    fake_scope = MagicMock()
    fake_scope.list_by_status.return_value = [
        {"domain": "quebra.com", "status": "REJECTED"},
        {"domain": "ok.com", "status": "REJECTED"},
    ]
    monkeypatch.setattr(sbm.brand_agent, "BrandScopedInvestigations", MagicMock(return_value=fake_scope))

    def _fake_record_rejection(*, brand_id, domain, investigation):
        if domain == "quebra.com":
            raise RuntimeError("falha simulada")
        return MagicMock()

    monkeypatch.setattr(sbm.brand_memory, "record_rejection", _fake_record_rejection)

    rejected_count, approved_count = sbm.sync_brand("nubank")

    assert rejected_count == 1  # so ok.com contou


def test_sync_all_iterates_monitored_brands(monkeypatch):
    monkeypatch.setattr(sbm, "MONITORED_BRANDS", ("nubank", "loggi"))
    fake_sync_brand = MagicMock(side_effect=[(1, 0), (0, 2)])
    monkeypatch.setattr(sbm, "sync_brand", fake_sync_brand)

    report = sbm.sync_all()

    assert report == {"nubank": (1, 0), "loggi": (0, 2)}
    assert fake_sync_brand.call_count == 2
