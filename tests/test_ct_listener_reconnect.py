"""Testes da Etapa C, item 4 -- reconexao do certstream com backoff
exponencial de verdade e medicao da lacuna de cobertura.

Contexto verificado nesta etapa (ver docstring de
`ct_listener._run_certstream_once`): a funcao publica do pacote
(`certstream.listen_for_events`) tem seu PROPRIO `while True: ...
time.sleep(5)` interno -- reconexoes comuns nunca voltavam a este
processo, entao o backoff exponencial "existente" antes desta etapa nunca
era exercitado numa queda comum. Estes testes cobrem o comportamento NOVO:
`CertStreamClient` chamado diretamente, com `on_open`/`on_error` reais."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

import plane1_ingestion.ct_listener as ctl


@pytest.fixture(autouse=True)
def _reset_globals(monkeypatch):
    monkeypatch.setattr(ctl, "_last_disconnect_monotonic", None)
    ctl._batch_deltas.clear()
    yield
    ctl._batch_deltas.clear()


# --- _flush_batch: dual-write para observation_run (Etapa C) ---------------


def test_flush_batch_bumps_observation_run_with_same_deltas(monkeypatch):
    ctl._firestore_db = MagicMock()
    monkeypatch.setattr(ctl.telemetry, "flush_metrics_to_firestore", MagicMock())
    fake_bump = MagicMock()
    fake_check = MagicMock()
    monkeypatch.setattr(ctl.observation_run, "bump", fake_bump)
    monkeypatch.setattr(ctl.observation_run, "check_prefilter_escape_anomaly", fake_check)

    ctl._batch_deltas["certificates_ingested_total"] = 7
    ctl._flush_batch()

    fake_bump.assert_called_once_with({"certificates_ingested_total": 7})
    fake_check.assert_called_once()
    assert ctl._batch_deltas == {}


def test_flush_batch_noop_with_empty_deltas(monkeypatch):
    fake_bump = MagicMock()
    monkeypatch.setattr(ctl.observation_run, "bump", fake_bump)
    ctl._flush_batch()
    fake_bump.assert_not_called()


# --- on_open/on_error: medicao da lacuna ------------------------------------


def test_on_certstream_open_without_prior_disconnect_just_logs(monkeypatch):
    fake_bump = MagicMock()
    monkeypatch.setattr(ctl.observation_run, "bump", fake_bump)

    ctl._on_certstream_open()

    fake_bump.assert_not_called()
    assert ctl._last_disconnect_monotonic is None


def test_on_certstream_error_marks_disconnect_start(monkeypatch):
    monkeypatch.setattr(ctl.time, "monotonic", lambda: 100.0)
    ctl._on_certstream_error(ConnectionError("queda"))
    assert ctl._last_disconnect_monotonic == 100.0


def test_on_certstream_error_does_not_overwrite_existing_gap_start(monkeypatch):
    ctl._last_disconnect_monotonic = 50.0
    monkeypatch.setattr(ctl.time, "monotonic", lambda: 999.0)
    ctl._on_certstream_error(ConnectionError("segundo erro antes de desistir"))
    assert ctl._last_disconnect_monotonic == 50.0


def test_on_certstream_open_after_disconnect_logs_gap_and_bumps_counters(monkeypatch, caplog):
    fake_bump = MagicMock()
    monkeypatch.setattr(ctl.observation_run, "bump", fake_bump)
    times = iter([100.0, 130.0])
    monkeypatch.setattr(ctl.time, "monotonic", lambda: next(times))

    ctl._on_certstream_error(ConnectionError("queda"))  # marca inicio em 100.0
    with caplog.at_level("WARNING", logger="ct_listener"):
        ctl._on_certstream_open()  # reconecta em 130.0 -- lacuna de 30s

    fake_bump.assert_called_once_with({"websocket_disconnects_total": 1, "websocket_gap_seconds_total": 30.0})
    assert ctl._last_disconnect_monotonic is None
    assert any("RECONECTADO" in r.message and "30.0" in r.message for r in caplog.records)


# --- _run_certstream_once: usa CertStreamClient diretamente, nao o loop
# interno de certstream.listen_for_events --------------------------------


def test_run_certstream_once_wires_real_callbacks(monkeypatch):
    fake_client_instance = MagicMock()
    fake_client_cls = MagicMock(return_value=fake_client_instance)
    monkeypatch.setattr(ctl, "CertStreamClient", fake_client_cls)

    ctl._run_certstream_once()

    fake_client_cls.assert_called_once_with(
        ctl.handle_certstream_message,
        ctl.CERTSTREAM_URL,
        on_open=ctl._on_certstream_open,
        on_error=ctl._on_certstream_error,
    )
    fake_client_instance.run_forever.assert_called_once_with(ping_interval=15)


# --- run_listener_with_reconnect: backoff exponencial de verdade -----------


@pytest.mark.asyncio
async def test_run_listener_with_reconnect_backs_off_exponentially(monkeypatch):
    monkeypatch.setattr(ctl, "_flush_batch", MagicMock())
    sleep_calls: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 4:
            raise asyncio.CancelledError()

    monkeypatch.setattr(ctl.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(ctl, "_run_certstream_once", MagicMock(side_effect=ConnectionError("queda")))

    with pytest.raises(asyncio.CancelledError):
        await ctl.run_listener_with_reconnect()

    # min=2, dobra a cada queda, teto em 60 (constantes do modulo).
    assert sleep_calls == [
        ctl._RECONNECT_MIN_DELAY_SECONDS,
        ctl._RECONNECT_MIN_DELAY_SECONDS * 2,
        ctl._RECONNECT_MIN_DELAY_SECONDS * 4,
        ctl._RECONNECT_MIN_DELAY_SECONDS * 8,
    ]


@pytest.mark.asyncio
async def test_run_listener_with_reconnect_caps_delay_at_max(monkeypatch):
    monkeypatch.setattr(ctl, "_flush_batch", MagicMock())
    sleep_calls: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 8:
            raise asyncio.CancelledError()

    monkeypatch.setattr(ctl.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(ctl, "_run_certstream_once", MagicMock(side_effect=ConnectionError("queda")))

    with pytest.raises(asyncio.CancelledError):
        await ctl.run_listener_with_reconnect()

    assert sleep_calls[-1] == ctl._RECONNECT_MAX_DELAY_SECONDS
    assert max(sleep_calls) == ctl._RECONNECT_MAX_DELAY_SECONDS
