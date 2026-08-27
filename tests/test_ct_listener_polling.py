"""Testes do LOOP EXTERNO de polling RFC 6962 em `ct_listener.py`
(`run_polling_loop`) -- cursor inicial, retomada, e backoff de get-sth.

A mecanica de PARALELISMO (faixas simultaneas, rodadas, controlador de
concorrencia) tem cobertura propria em `test_ct_listener_parallel_ingestion.py`
-- aqui `_run_parallel_round` e mockado para isolar o comportamento da
CASCA do loop (o que decide cursor inicial, quando dormir, como reagir a
get-sth falhando) da mecanica de dentro de uma rodada."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import plane1_ingestion.ct_listener as ctl
from config import settings
from plane1_ingestion import ct_rfc6962


class _StopLoop(asyncio.CancelledError):
    """Subclasse so para deixar claro, na leitura do teste, que o
    cancelamento e o MECANISMO de parada deliberado, nao um erro real."""


def _stop_after(n: int):
    calls: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        calls.append(seconds)
        if len(calls) >= n:
            raise _StopLoop()

    return calls, _fake_sleep


# --- Cursor inicial = tree_size, nunca 0 -------------------------------------


@pytest.mark.asyncio
async def test_first_boot_starts_from_tree_size_not_zero(monkeypatch):
    monkeypatch.setattr(ctl.observation_run, "load_ct_cursor", lambda: None)
    fetch_sth_mock = MagicMock(return_value=ct_rfc6962.SignedTreeHead(tree_size=1_000_000, timestamp=1))
    monkeypatch.setattr(ctl.ct_rfc6962, "fetch_sth", fetch_sth_mock)
    round_mock = AsyncMock()
    monkeypatch.setattr(ctl, "_run_parallel_round", round_mock)

    calls, fake_sleep = _stop_after(1)
    monkeypatch.setattr(ctl.asyncio, "sleep", fake_sleep)

    with pytest.raises(_StopLoop):
        await ctl.run_polling_loop()

    # cursor==tree_size logo no primeiro ciclo -- nunca chama uma rodada,
    # cai direto no sleep de "em dia com o log".
    round_mock.assert_not_called()
    assert calls == [settings.ct_poll_interval_seconds]
    assert fetch_sth_mock.call_count == 2  # 1 para fixar o cursor inicial + 1 no loop


# --- Retomada do cursor persistido -------------------------------------------


@pytest.mark.asyncio
async def test_resumes_from_persisted_cursor_not_from_tree_size(monkeypatch):
    monkeypatch.setattr(ctl.observation_run, "load_ct_cursor", lambda: 500)
    fetch_sth_mock = MagicMock(return_value=ct_rfc6962.SignedTreeHead(tree_size=600, timestamp=1))
    monkeypatch.setattr(ctl.ct_rfc6962, "fetch_sth", fetch_sth_mock)

    round_mock = AsyncMock(return_value=600)  # rodada "termina" no tree_size
    monkeypatch.setattr(ctl, "_run_parallel_round", round_mock)

    calls, fake_sleep = _stop_after(1)
    monkeypatch.setattr(ctl.asyncio, "sleep", fake_sleep)

    with pytest.raises(_StopLoop):
        await ctl.run_polling_loop()

    # Primeira chamada de rodada usa o cursor PERSISTIDO (500), nunca o
    # tree_size (600) como ponto de partida.
    round_mock.assert_called_once()
    assert round_mock.call_args.args[0] == 500
    assert round_mock.call_args.args[1] == 600


# --- Em dia com o log: dorme no ritmo normal, nao chama rodada ---------------


@pytest.mark.asyncio
async def test_caught_up_sleeps_at_poll_interval_without_running_a_round(monkeypatch):
    monkeypatch.setattr(ctl.observation_run, "load_ct_cursor", lambda: 1000)
    monkeypatch.setattr(
        ctl.ct_rfc6962, "fetch_sth", MagicMock(return_value=ct_rfc6962.SignedTreeHead(tree_size=1000, timestamp=1))
    )
    round_mock = AsyncMock()
    monkeypatch.setattr(ctl, "_run_parallel_round", round_mock)

    calls, fake_sleep = _stop_after(1)
    monkeypatch.setattr(ctl.asyncio, "sleep", fake_sleep)

    with pytest.raises(_StopLoop):
        await ctl.run_polling_loop()

    round_mock.assert_not_called()
    assert calls == [settings.ct_poll_interval_seconds]


# --- get-sth transitorio: backoff exponencial, sem crashar -------------------


@pytest.mark.asyncio
async def test_get_sth_transient_error_backs_off_then_recovers(monkeypatch):
    monkeypatch.setattr(ctl.observation_run, "load_ct_cursor", lambda: 100)
    sth_ok = ct_rfc6962.SignedTreeHead(tree_size=100, timestamp=1)  # ja em dia -- so testa o backoff do get-sth
    fetch_sth_mock = MagicMock(side_effect=[ct_rfc6962.CTLogUnavailableError("timeout"), sth_ok])
    monkeypatch.setattr(ctl.ct_rfc6962, "fetch_sth", fetch_sth_mock)
    monkeypatch.setattr(ctl, "_run_parallel_round", AsyncMock())

    calls, fake_sleep = _stop_after(2)
    monkeypatch.setattr(ctl.asyncio, "sleep", fake_sleep)

    with pytest.raises(_StopLoop):
        await ctl.run_polling_loop()

    assert calls == [settings.ct_http_backoff_min_seconds, settings.ct_poll_interval_seconds]


@pytest.mark.asyncio
async def test_repeated_get_sth_errors_back_off_exponentially_and_cap(monkeypatch):
    monkeypatch.setattr(ctl.observation_run, "load_ct_cursor", lambda: 100)
    monkeypatch.setattr(
        ctl.ct_rfc6962, "fetch_sth", MagicMock(side_effect=ct_rfc6962.CTLogUnavailableError("fora do ar"))
    )
    monkeypatch.setattr(ctl, "_run_parallel_round", AsyncMock())

    calls, fake_sleep = _stop_after(10)
    monkeypatch.setattr(ctl.asyncio, "sleep", fake_sleep)

    with pytest.raises(_StopLoop):
        await ctl.run_polling_loop()

    assert calls[:4] == [
        settings.ct_http_backoff_min_seconds,
        settings.ct_http_backoff_min_seconds * 2,
        settings.ct_http_backoff_min_seconds * 4,
        settings.ct_http_backoff_min_seconds * 8,
    ]
    assert calls[-1] == settings.ct_http_backoff_max_seconds
    assert max(calls) == settings.ct_http_backoff_max_seconds


# --- O cursor devolvido pela rodada realimenta a proxima iteracao -----------


@pytest.mark.asyncio
async def test_cursor_returned_by_round_feeds_next_iteration(monkeypatch):
    monkeypatch.setattr(ctl.observation_run, "load_ct_cursor", lambda: 0)
    monkeypatch.setattr(
        ctl.ct_rfc6962, "fetch_sth", MagicMock(return_value=ct_rfc6962.SignedTreeHead(tree_size=1000, timestamp=1))
    )
    # 1a rodada avanca de 0 para 400 (nao chegou ao tree_size ainda);
    # 2a rodada recebe 400 como cursor de entrada.
    round_mock = AsyncMock(side_effect=[400, 1000])
    monkeypatch.setattr(ctl, "_run_parallel_round", round_mock)

    calls, fake_sleep = _stop_after(1)
    monkeypatch.setattr(ctl.asyncio, "sleep", fake_sleep)

    with pytest.raises(_StopLoop):
        await ctl.run_polling_loop()

    assert round_mock.call_args_list[0].args[0] == 0
    assert round_mock.call_args_list[1].args[0] == 400
