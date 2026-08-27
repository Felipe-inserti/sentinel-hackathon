"""Testes de `observation_run.py` (Etapa C -- instrumentacao do run de
observacao de 48h). Firestore sempre mockado (mesmo padrao de
tests/test_takedown_agent.py::test_check_and_increment_rate_limit_*).

Cobre, na ordem de prioridade do pedido:

  1. Guarda de custo: no-op fora de um run ativo; recusa (loga CRITICAL)
     quando o gasto acumulado (Gemini + Gemma) ja bateu o teto; permite
     abaixo do teto.
  2. `bump()`: no-op sem run ativo/sem deltas; incremento atomico
     (`firestore.Increment`) quando ativo, sempre carimbando `run_id`/
     `last_updated_at`; `started_at` gravado uma unica vez (nao
     sobrescrito numa segunda chamada).
  3. Checkpoint: no-op sem run ativo; grava snapshot com timestamp na
     subcolecao `checkpoints` quando ativo; `checkpoint_loop` retorna
     IMEDIATAMENTE (sem laco) quando inativo.
  5. Alerta de anomalia: no-op abaixo da amostra minima; CRITICAL acima do
     limiar configurado; silencioso abaixo dele.
  6. Trava de DRY_RUN: recusa iniciar quando um run esta ativo com
     DRY_RUN=false; permite nos outros tres casos (run inativo, ou
     DRY_RUN=true)."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock

import pytest

import observation_run as obs


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    """Isola cada teste: nenhum run ativo por default, sem cliente
    Firestore real, sem a flag `started_at` vazando entre testes."""
    monkeypatch.setattr(obs.settings, "observation_run_id", None)
    monkeypatch.setattr(obs, "_db", None)
    monkeypatch.setattr(obs, "_started_at_checked", False)
    yield


def _activate(monkeypatch, run_id: str = "obs-test") -> None:
    monkeypatch.setattr(obs.settings, "observation_run_id", run_id)


# --- is_active / no-op geral fora de um run ---------------------------------


def test_is_active_false_by_default():
    assert obs.is_active() is False


def test_is_active_true_when_run_id_set(monkeypatch):
    _activate(monkeypatch)
    assert obs.is_active() is True


def test_bump_is_noop_without_active_run(monkeypatch):
    fake_db = MagicMock()
    monkeypatch.setattr(obs, "_db", fake_db)
    obs.bump({"certificates_ingested_total": 5})
    fake_db.collection.assert_not_called()


def test_bump_is_noop_with_empty_deltas(monkeypatch):
    _activate(monkeypatch)
    fake_db = MagicMock()
    monkeypatch.setattr(obs, "_db", fake_db)
    obs.bump({})
    fake_db.collection.assert_not_called()


def test_get_totals_empty_without_active_run():
    assert obs.get_totals() == {}


# --- bump(): incremento atomico ---------------------------------------------


def test_bump_writes_increment_and_stamps_run_id(monkeypatch):
    _activate(monkeypatch, "obs-2026-08-28")
    monkeypatch.setattr(obs.firestore, "transactional", lambda fn: fn)
    fake_db = MagicMock()
    monkeypatch.setattr(obs, "_db", fake_db)
    doc_ref = fake_db.collection.return_value.document.return_value
    doc_ref.get.return_value = MagicMock(exists=True, to_dict=lambda: {"started_at": "ja-existe"})

    obs.bump({"certificates_ingested_total": 3, "estimated_cost_usd_total": 0.001})

    fake_db.collection.assert_called_with(obs.settings.observation_runs_collection)
    fake_db.collection.return_value.document.assert_called_with("obs-2026-08-28")
    doc_ref.set.assert_called_once()
    written, kwargs = doc_ref.set.call_args
    payload = written[0]
    assert isinstance(payload["certificates_ingested_total"], obs.firestore.Increment)
    assert payload["run_id"] == "obs-2026-08-28"
    assert "last_updated_at" in payload
    assert kwargs["merge"] is True


def test_bump_never_raises_when_firestore_fails(monkeypatch):
    _activate(monkeypatch)
    monkeypatch.setattr(obs.firestore, "transactional", lambda fn: fn)
    fake_db = MagicMock()
    fake_db.collection.return_value.document.return_value.get.side_effect = Exception("boom")
    fake_db.collection.return_value.document.return_value.set.side_effect = Exception("boom")
    monkeypatch.setattr(obs, "_db", fake_db)

    obs.bump({"certificates_ingested_total": 1})  # nao deve levantar


def test_started_at_written_once_not_overwritten(monkeypatch):
    """Segunda chamada de bump() nao deve tentar regravar started_at se o
    documento ja tem o campo (a transacao verifica 'nao existe' antes de
    escrever) -- aqui verificamos que a segunda vez, com o doc ja tendo
    started_at, a transacao nao tenta setar de novo (so o bump() em si
    grava, via set() fora da transacao)."""
    _activate(monkeypatch)
    monkeypatch.setattr(obs.firestore, "transactional", lambda fn: fn)
    fake_db = MagicMock()
    monkeypatch.setattr(obs, "_db", fake_db)
    doc_ref = fake_db.collection.return_value.document.return_value
    fake_transaction = MagicMock()
    fake_db.transaction.return_value = fake_transaction

    # Primeira chamada: documento ainda nao existe -- started_at deve ser
    # gravado pela transacao.
    doc_ref.get.return_value = MagicMock(exists=False, to_dict=lambda: None)
    obs.bump({"certificates_ingested_total": 1})
    assert fake_transaction.set.call_count == 1
    assert "started_at" in fake_transaction.set.call_args[0][1]

    # _started_at_checked agora e True -- uma segunda chamada NAO tenta a
    # transacao de novo (otimizacao por processo, ver docstring do modulo).
    obs.bump({"certificates_ingested_total": 1})
    assert fake_transaction.set.call_count == 1


# --- guarda de custo (item 1) -----------------------------------------------


def test_cost_guard_allows_when_no_run_active():
    assert obs.cost_guard_allows_llm_call() is True


def test_cost_guard_allows_below_limit(monkeypatch):
    _activate(monkeypatch)
    monkeypatch.setattr(obs.settings, "observation_cost_guard_usd_limit", 10.0)
    monkeypatch.setattr(
        obs,
        "get_totals",
        lambda: {"estimated_cost_usd_total": 3.0, "gemma_triage_cost_usd_total": 0.5},
    )
    assert obs.cost_guard_allows_llm_call() is True


def test_cost_guard_blocks_at_or_above_limit_and_logs_critical(monkeypatch, caplog):
    _activate(monkeypatch)
    monkeypatch.setattr(obs.settings, "observation_cost_guard_usd_limit", 10.0)
    monkeypatch.setattr(
        obs,
        "get_totals",
        lambda: {"estimated_cost_usd_total": 9.5, "gemma_triage_cost_usd_total": 0.6},
    )
    with caplog.at_level(logging.CRITICAL, logger="observation_run"):
        allowed = obs.cost_guard_allows_llm_call()

    assert allowed is False
    assert any(r.levelno == logging.CRITICAL for r in caplog.records)


def test_cost_guard_combines_gemini_and_gemma_cost(monkeypatch):
    """O teto e sobre GEMINI + GEMMA somados, nao so um dos dois."""
    _activate(monkeypatch)
    monkeypatch.setattr(obs.settings, "observation_cost_guard_usd_limit", 1.0)
    monkeypatch.setattr(
        obs, "get_totals", lambda: {"estimated_cost_usd_total": 0.6, "gemma_triage_cost_usd_total": 0.6}
    )
    assert obs.cost_guard_allows_llm_call() is False


# --- checkpoint (item 3) -----------------------------------------------------


def test_record_checkpoint_noop_without_active_run(monkeypatch):
    fake_db = MagicMock()
    monkeypatch.setattr(obs, "_db", fake_db)
    obs.record_checkpoint()
    fake_db.collection.assert_not_called()


def test_record_checkpoint_writes_snapshot_with_timestamp(monkeypatch):
    _activate(monkeypatch)
    monkeypatch.setattr(obs, "get_totals", lambda: {"certificates_ingested_total": 42})
    fake_db = MagicMock()
    monkeypatch.setattr(obs, "_db", fake_db)

    obs.record_checkpoint()

    checkpoints_collection = (
        fake_db.collection.return_value.document.return_value.collection.return_value
    )
    checkpoints_collection.document.assert_called_once()
    doc_ref = checkpoints_collection.document.return_value
    doc_ref.set.assert_called_once()
    payload = doc_ref.set.call_args[0][0]
    assert payload["certificates_ingested_total"] == 42
    assert "checkpoint_at" in payload


@pytest.mark.asyncio
async def test_checkpoint_loop_returns_immediately_when_inactive():
    # Sem run ativo -- deve retornar sem nunca chamar asyncio.sleep (senao
    # este teste travaria/estouraria o timeout).
    await asyncio.wait_for(obs.checkpoint_loop(), timeout=1.0)


# --- cursor do polling RFC 6962 (troca do certstream) ------------------------


def test_save_ct_cursor_is_noop_without_active_run(monkeypatch):
    fake_db = MagicMock()
    monkeypatch.setattr(obs, "_db", fake_db)
    obs.save_ct_cursor(12345)
    fake_db.collection.assert_not_called()


def test_load_ct_cursor_returns_none_without_active_run():
    assert obs.load_ct_cursor() is None


def test_load_ct_cursor_returns_none_when_never_saved(monkeypatch):
    _activate(monkeypatch)
    monkeypatch.setattr(obs, "get_totals", lambda: {})
    assert obs.load_ct_cursor() is None


def test_save_ct_cursor_writes_absolute_value_not_increment(monkeypatch):
    """Ao contrario de bump(), isto e um VALOR (proximo indice), nao um
    contador -- nao pode usar firestore.Increment."""
    _activate(monkeypatch, "obs-2026-08-27")
    fake_db = MagicMock()
    monkeypatch.setattr(obs, "_db", fake_db)
    doc_ref = fake_db.collection.return_value.document.return_value

    obs.save_ct_cursor(999_888_777)

    doc_ref.set.assert_called_once()
    payload, kwargs = doc_ref.set.call_args
    assert payload[0][obs._CT_CURSOR_FIELD] == 999_888_777
    assert not isinstance(payload[0][obs._CT_CURSOR_FIELD], obs.firestore.Increment)
    assert payload[0]["run_id"] == "obs-2026-08-27"
    assert kwargs["merge"] is True


def test_save_ct_cursor_never_raises_when_firestore_fails(monkeypatch):
    _activate(monkeypatch)
    fake_db = MagicMock()
    fake_db.collection.return_value.document.return_value.set.side_effect = Exception("boom")
    monkeypatch.setattr(obs, "_db", fake_db)
    obs.save_ct_cursor(1)  # nao deve levantar


def test_load_ct_cursor_round_trips_saved_value(monkeypatch):
    _activate(monkeypatch)
    monkeypatch.setattr(obs, "get_totals", lambda: {obs._CT_CURSOR_FIELD: 42})
    assert obs.load_ct_cursor() == 42


# --- alerta de anomalia (item 5) --------------------------------------------


def test_anomaly_check_noop_without_active_run(caplog):
    with caplog.at_level(logging.CRITICAL, logger="observation_run"):
        obs.check_prefilter_escape_anomaly()
    assert caplog.records == []


def test_anomaly_check_skips_below_minimum_sample(monkeypatch, caplog):
    _activate(monkeypatch)
    monkeypatch.setattr(obs.settings, "observation_anomaly_min_sample_size", 200)
    monkeypatch.setattr(
        obs,
        "get_totals",
        lambda: {"certificates_ingested_total": 5, "certificates_discarded_by_prefilter_total": 0},
    )
    with caplog.at_level(logging.CRITICAL, logger="observation_run"):
        obs.check_prefilter_escape_anomaly()
    assert caplog.records == []


def test_anomaly_check_triggers_above_threshold(monkeypatch, caplog):
    _activate(monkeypatch)
    monkeypatch.setattr(obs.settings, "observation_anomaly_min_sample_size", 100)
    monkeypatch.setattr(obs.settings, "observation_prefilter_escape_rate_threshold", 0.05)
    monkeypatch.setattr(
        obs,
        "get_totals",
        lambda: {"certificates_ingested_total": 1000, "certificates_discarded_by_prefilter_total": 900},
    )
    with caplog.at_level(logging.CRITICAL, logger="observation_run"):
        obs.check_prefilter_escape_anomaly()
    assert any(r.levelno == logging.CRITICAL for r in caplog.records)


def test_anomaly_check_silent_below_threshold(monkeypatch, caplog):
    _activate(monkeypatch)
    monkeypatch.setattr(obs.settings, "observation_anomaly_min_sample_size", 100)
    monkeypatch.setattr(obs.settings, "observation_prefilter_escape_rate_threshold", 0.05)
    monkeypatch.setattr(
        obs,
        "get_totals",
        lambda: {"certificates_ingested_total": 1000, "certificates_discarded_by_prefilter_total": 990},
    )
    with caplog.at_level(logging.CRITICAL, logger="observation_run"):
        obs.check_prefilter_escape_anomaly()
    assert caplog.records == []


# --- trava de DRY_RUN (item 6) ----------------------------------------------


def test_enforce_dry_run_lock_noop_without_active_run(monkeypatch):
    monkeypatch.setattr(obs.settings, "dry_run", False)
    obs.enforce_dry_run_lock()  # nao levanta -- observacao nem esta ativa


def test_enforce_dry_run_lock_allows_when_dry_run_true(monkeypatch):
    _activate(monkeypatch)
    monkeypatch.setattr(obs.settings, "dry_run", True)
    obs.enforce_dry_run_lock()  # nao levanta


def test_enforce_dry_run_lock_blocks_when_active_and_dry_run_false(monkeypatch):
    _activate(monkeypatch, "obs-2026-08-28")
    monkeypatch.setattr(obs.settings, "dry_run", False)
    with pytest.raises(RuntimeError, match="DRY_RUN"):
        obs.enforce_dry_run_lock()
