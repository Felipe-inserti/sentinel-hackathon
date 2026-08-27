"""Testes da ingestao PARALELA (multiplas faixas de indice RFC 6962
simultaneas) em `ct_listener.py` -- `_plan_windows`, `_drain_window`,
`_run_parallel_round`, `_ConcurrencyController`.

Cobre os tres requisitos explicitos do pedido:
  1. Faixa que falha no meio -- retry na MESMA posicao, nunca pula.
  2. Ordem de conclusao fora de sequencia -- uma faixa mais rapida (indice
     maior) nao pode fazer o cursor avancar enquanto uma faixa mais lenta
     (indice menor) ainda nao terminou.
  3. Cursor nao avanca alem de uma lacuna -- uma faixa presa (nunca
     termina) impede QUALQUER persistencia de cursor pela rodada inteira.

Mais o comportamento de subir/descer concorrencia (sobe 1 por rodada limpa,
reduz pela metade no primeiro 429 -- nunca aumenta e reduz na MESMA
rodada)."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import plane1_ingestion.ct_listener as ctl
from plane1_ingestion import ct_rfc6962


def _entries(n: int) -> list[dict]:
    return [{"leaf_input": "x", "extra_data": ""}] * n


# --- _plan_windows ------------------------------------------------------


def test_plan_windows_splits_into_contiguous_non_overlapping_ranges():
    windows = ctl._plan_windows(cursor=0, tree_size=250, concurrency=3, window_span=100)
    assert windows == [(0, 99), (100, 199), (200, 249)]


def test_plan_windows_stops_early_when_backlog_smaller_than_full_concurrency():
    windows = ctl._plan_windows(cursor=0, tree_size=50, concurrency=5, window_span=100)
    assert windows == [(0, 49)]


def test_plan_windows_empty_when_already_caught_up():
    assert ctl._plan_windows(cursor=100, tree_size=100, concurrency=4, window_span=100) == []


# --- _ConcurrencyController -----------------------------------------------


def test_controller_ramps_up_by_one_on_clean_round():
    c = ctl._ConcurrencyController(current=2, minimum=1, maximum=8)
    c.note_round_result(rate_limited=False)
    assert c.current == 3


def test_controller_caps_at_maximum():
    c = ctl._ConcurrencyController(current=8, minimum=1, maximum=8)
    c.note_round_result(rate_limited=False)
    assert c.current == 8


def test_controller_halves_on_rate_limited_round():
    c = ctl._ConcurrencyController(current=8, minimum=1, maximum=8)
    c.note_round_result(rate_limited=True)
    assert c.current == 4


def test_controller_floors_at_minimum_when_halving():
    c = ctl._ConcurrencyController(current=1, minimum=1, maximum=8)
    c.note_round_result(rate_limited=True)
    assert c.current == 1


# --- _drain_window: uma faixa so ------------------------------------------


@pytest.mark.asyncio
async def test_drain_window_advances_by_actual_count_across_multiple_calls(monkeypatch):
    monkeypatch.setattr(ctl.asyncio, "sleep", AsyncMock())
    calls: list[tuple[int, int]] = []

    def fake_fetch_entries(start, end):
        calls.append((start, end))
        remaining = end - start + 1
        return _entries(min(20, remaining))

    monkeypatch.setattr(ctl.ct_rfc6962, "fetch_entries", MagicMock(side_effect=fake_fetch_entries))
    monkeypatch.setattr(ctl.ct_rfc6962, "parse_leaf_entry", MagicMock(return_value=None))

    flag = [False]
    await ctl._drain_window(0, 49, flag)  # 50 indices, 20/chamada -> 3 chamadas

    assert calls == [(0, 49), (20, 49), (40, 49)]
    assert flag[0] is False


@pytest.mark.asyncio
async def test_drain_window_retries_transient_error_at_same_position_never_skipping(monkeypatch):
    """Requisito 1: faixa que falha no meio -- o retry pede EXATAMENTE a
    mesma posicao que falhou, nunca avanca sem ter recebido dado."""
    monkeypatch.setattr(ctl.asyncio, "sleep", AsyncMock())
    calls: list[tuple[int, int]] = []

    def fake_fetch_entries(start, end):
        calls.append((start, end))
        if len(calls) == 1:
            raise ct_rfc6962.CTLogUnavailableError("erro transitorio no meio da faixa")
        return _entries(50)

    monkeypatch.setattr(ctl.ct_rfc6962, "fetch_entries", MagicMock(side_effect=fake_fetch_entries))
    monkeypatch.setattr(ctl.ct_rfc6962, "parse_leaf_entry", MagicMock(return_value=None))

    flag = [False]
    await ctl._drain_window(0, 49, flag)

    assert calls == [(0, 49), (0, 49)]  # 2a tentativa NAO pulou para frente
    assert flag[0] is False


@pytest.mark.asyncio
async def test_drain_window_sets_rate_limited_flag_on_429(monkeypatch):
    monkeypatch.setattr(ctl.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(
        ctl.ct_rfc6962,
        "fetch_entries",
        MagicMock(side_effect=[ct_rfc6962.CTLogRateLimitedError("429"), _entries(10)]),
    )
    monkeypatch.setattr(ctl.ct_rfc6962, "parse_leaf_entry", MagicMock(return_value=None))

    flag = [False]
    await ctl._drain_window(0, 9, flag)

    assert flag[0] is True


# --- _run_parallel_round: garantias sob paralelismo -----------------------


@pytest.mark.asyncio
async def test_round_persists_cursor_only_after_all_windows_complete_even_out_of_order(monkeypatch):
    """Requisito 2: janela B (indice maior) termina numa chamada so; janela
    A (indice menor) precisa de duas chamadas e leva mais tempo de
    verdade (time.sleep real dentro do fake, nao so mockado) -- B termina
    "antes" de A. O cursor so pode avancar UMA VEZ, para o fim da rodada
    inteira, nunca para o fim de B enquanto A ainda nao tinha terminado."""
    monkeypatch.setattr(ctl.asyncio, "sleep", AsyncMock())
    # Forca 2 janelas de 100 indices cada (0-99, 100-199) -- sem isto o
    # window_span default (1000) cobriria os 200 indices numa janela SO.
    monkeypatch.setattr(ctl.settings, "ct_get_entries_request_size", 100)

    completion_order: list[int] = []
    real_drain_window = ctl._drain_window

    async def spy_drain_window(start, end, flag):
        await real_drain_window(start, end, flag)
        completion_order.append(start)

    monkeypatch.setattr(ctl, "_drain_window", spy_drain_window)

    def fake_fetch_entries(start, end):
        if start < 100:  # janela A -- duas chamadas, com atraso real de verdade
            time.sleep(0.02)
            return _entries(30) if start == 0 else _entries(70)
        return _entries(100)  # janela B -- uma chamada so, sem atraso

    monkeypatch.setattr(ctl.ct_rfc6962, "fetch_entries", MagicMock(side_effect=fake_fetch_entries))
    monkeypatch.setattr(ctl.ct_rfc6962, "parse_leaf_entry", MagicMock(return_value=None))
    save_mock = MagicMock()
    monkeypatch.setattr(ctl.observation_run, "save_ct_cursor", save_mock)

    controller = ctl._ConcurrencyController(current=2, minimum=1, maximum=8)
    new_cursor = await ctl._run_parallel_round(0, 200, controller)

    # B (start=100) de fato terminou antes de A (start=0) -- concorrencia
    # de verdade, nao so paralelismo nominal.
    assert completion_order == [100, 0]

    # Mas so existe UMA persistencia de cursor, para o fim da rodada
    # inteira (200) -- nunca 200 (fim de B) gravado enquanto A pendente.
    save_mock.assert_called_once_with(200)
    assert new_cursor == 200

    # Rodada inteira sem 429 -- concorrencia sobe.
    assert controller.current == 3


@pytest.mark.asyncio
async def test_round_never_persists_cursor_while_one_window_is_permanently_stuck(monkeypatch):
    """Requisito 3: janela A nunca consegue ler (erro transitorio sem
    fim) -- a rodada NUNCA termina, e portanto NUNCA persiste cursor
    nenhum, mesmo com a janela B pronta ha muito tempo. O gap nunca e
    pulado silenciosamente."""
    monkeypatch.setattr(ctl.asyncio, "sleep", AsyncMock())
    save_mock = MagicMock()
    monkeypatch.setattr(ctl.observation_run, "save_ct_cursor", save_mock)
    monkeypatch.setattr(ctl.ct_rfc6962, "parse_leaf_entry", MagicMock(return_value=None))

    def fake_fetch_entries(start, end):
        if start < 100:
            raise ct_rfc6962.CTLogUnavailableError("faixa permanentemente presa")
        return _entries(100)

    monkeypatch.setattr(ctl.ct_rfc6962, "fetch_entries", MagicMock(side_effect=fake_fetch_entries))

    controller = ctl._ConcurrencyController(current=2, minimum=1, maximum=8)

    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await asyncio.wait_for(ctl._run_parallel_round(0, 200, controller), timeout=0.2)

    save_mock.assert_not_called()
    # A rodada nunca terminou -- o controlador tambem nunca e atualizado
    # (nao faz sentido "ramp up" uma rodada que nunca fechou).
    assert controller.current == 2


@pytest.mark.asyncio
async def test_round_reduces_concurrency_when_any_window_saw_a_429(monkeypatch):
    monkeypatch.setattr(ctl.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(ctl.settings, "ct_get_entries_request_size", 50)
    save_mock = MagicMock()
    monkeypatch.setattr(ctl.observation_run, "save_ct_cursor", save_mock)
    monkeypatch.setattr(ctl.ct_rfc6962, "parse_leaf_entry", MagicMock(return_value=None))

    attempts: dict[int, int] = {}

    def fake_fetch_entries(start, end):
        attempts[start] = attempts.get(start, 0) + 1
        if start == 50 and attempts[start] == 1:
            raise ct_rfc6962.CTLogRateLimitedError("429")
        return _entries(end - start + 1)

    monkeypatch.setattr(ctl.ct_rfc6962, "fetch_entries", MagicMock(side_effect=fake_fetch_entries))

    controller = ctl._ConcurrencyController(current=4, minimum=1, maximum=8)
    new_cursor = await ctl._run_parallel_round(0, 100, controller)

    assert new_cursor == 100
    save_mock.assert_called_once_with(100)
    assert controller.current == 2  # 4 // 2 -- reduzida por causa do 429, nao subiu
