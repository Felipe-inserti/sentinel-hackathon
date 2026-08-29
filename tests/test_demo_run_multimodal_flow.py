"""Testes de `demo_run_multimodal_flow.py` -- script de demo ponta a
ponta (Sprint multimodal). Todas as chamadas externas mockadas (Gemini,
Firestore, evidence_agent, takedown_agent) -- este teste garante que o
FLUXO de decisao do script (quando para, quando aprova, quando envia) esta
correto, nao que o Gemini/GCP real funcionam."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import demo_run_multimodal_flow as drmf


def _analysis_result(classification: str, **overrides):
    import plane2_agents.orchestrator as orch

    defaults = dict(classification=classification, confidence=0.9, reasoning="teste")
    defaults.update(overrides)
    return orch.AnalysisResult(**defaults)


def _usage(**overrides):
    from llm_client import LLMUsage

    defaults = dict(model_id="gemini-teste", input_tokens=100, output_tokens=20, latency_ms=1.0)
    defaults.update(overrides)
    return LLMUsage(**defaults)


def _sanitized(**overrides):
    from sanitizer import SanitizationResult

    defaults = dict(clean_text="ok", injection_patterns_found=[], pii_redacted={})
    defaults.update(overrides)
    return SanitizationResult(**defaults)


@pytest.mark.asyncio
async def test_safe_verdict_stops_after_step_1(monkeypatch, capsys):
    import plane2_agents.orchestrator as orch

    fake_classify = AsyncMock(
        return_value=(_analysis_result("SAFE"), _usage(), _sanitized(), 0.0005, orch.BrandMemoryUsage(0, 0, 0.0))
    )
    monkeypatch.setattr(orch, "classify_domain_with_gemini", fake_classify)

    with patch.object(sys, "argv", ["demo_run_multimodal_flow.py", "banco-teste-fake.local"]):
        exit_code = await drmf.main()

    assert exit_code == 0
    fake_classify.assert_awaited_once()
    out = capsys.readouterr().out
    assert "SAFE" in out
    assert "MALICIOUS" not in out or "nao classificado" in out.lower() or "nada mais a fazer" in out.lower()


@pytest.mark.asyncio
async def test_malicious_without_simulate_approval_stops_before_firestore(monkeypatch, capsys):
    import plane2_agents.orchestrator as orch

    fake_classify = AsyncMock(
        return_value=(
            _analysis_result(
                "MALICIOUS",
                brand_impersonated="BancoTeste",
                credential_form_present=True,
                visual_analysis_available=True,
            ),
            _usage(),
            _sanitized(),
            0.001,
            orch.BrandMemoryUsage(0, 0, 0.0),
        )
    )
    monkeypatch.setattr(orch, "classify_domain_with_gemini", fake_classify)

    with patch.object(sys, "argv", ["demo_run_multimodal_flow.py", "banco-teste-fake.local", "--skip-evidence"]):
        with patch("google.cloud.firestore.Client") as fake_firestore_client:
            exit_code = await drmf.main()

    assert exit_code == 0
    fake_firestore_client.assert_not_called()  # nunca grava aprovacao sem --simulate-approval
    out = capsys.readouterr().out
    assert "dashboard" in out.lower()


@pytest.mark.asyncio
async def test_malicious_with_simulate_approval_writes_firestore_and_runs_takedown(monkeypatch, capsys):
    import plane2_agents.orchestrator as orch

    fake_classify = AsyncMock(
        return_value=(
            _analysis_result(
                "MALICIOUS",
                brand_impersonated="BancoTeste",
                credential_form_present=True,
                visual_analysis_available=True,
            ),
            _usage(),
            _sanitized(),
            0.001,
            orch.BrandMemoryUsage(0, 0, 0.0),
        )
    )
    monkeypatch.setattr(orch, "classify_domain_with_gemini", fake_classify)

    fake_doc_ref = MagicMock()
    fake_db = MagicMock()
    fake_db.collection.return_value.document.return_value = fake_doc_ref

    fake_output = MagicMock(sent=True, dry_run=False)
    fake_process = AsyncMock(return_value=fake_output)

    with patch.object(
        sys, "argv", ["demo_run_multimodal_flow.py", "banco-teste-fake.local", "--skip-evidence", "--simulate-approval"]
    ):
        with patch("google.cloud.firestore.Client", return_value=fake_db):
            with patch("takedown_agent.process_takedown_approval", fake_process):
                with patch("config.settings.demo_live_send_allowlist", {"banco-teste-fake.local": "eu@exemplo.test"}):
                    exit_code = await drmf.main()

    assert exit_code == 0
    fake_doc_ref.set.assert_called_once()
    saved = fake_doc_ref.set.call_args[0][0]
    assert saved["status"] == "TAKEDOWN_APPROVED"
    assert "SIMULADO" in saved["approved_by"]
    assert saved["takedown_channel"] == "registrar_abuse"
    fake_process.assert_awaited_once()
    out = capsys.readouterr().out
    assert "enviado" in out.lower()


@pytest.mark.asyncio
async def test_evidence_collection_failure_never_aborts_the_demo(monkeypatch, capsys):
    """`evidence_agent.collect_evidence` pode falhar (ex: bucket GCS ainda
    nao existe) -- o script continua para a aprovacao/e-mail, so avisa."""
    import plane2_agents.orchestrator as orch

    fake_classify = AsyncMock(
        return_value=(
            _analysis_result("MALICIOUS", visual_analysis_available=False),
            _usage(),
            _sanitized(),
            0.001,
            orch.BrandMemoryUsage(0, 0, 0.0),
        )
    )
    monkeypatch.setattr(orch, "classify_domain_with_gemini", fake_classify)

    with patch.object(sys, "argv", ["demo_run_multimodal_flow.py", "banco-teste-fake.local"]):
        with patch("evidence_agent.collect_evidence", AsyncMock(side_effect=RuntimeError("bucket nao existe"))):
            exit_code = await drmf.main()

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "NAO VERIFICADO" in out
    assert "dashboard" in out.lower()  # segue ate a instrucao de aprovacao
