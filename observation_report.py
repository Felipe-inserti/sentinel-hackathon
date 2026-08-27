#!/usr/bin/env python3
"""Etapa C, item 7 -- relatorio final do run de observacao de 48h contra o
Certificate Transparency real.

Reusa a MESMA matematica de funil/custo de `metrics_report.py`
(`compute_report`/`compute_funnel`/`render_funnel`/`render_report`) -- so
troca a FONTE dos totais: em vez do documento global
`metrics/pipeline_totals` (vida inteira do projeto, todos os runs
misturados de uma vez), le `observation_runs/{run_id}` (ver
observation_run.py), escopado a UM run especifico e resistente a
re-disparo do Cloud Scheduler. Os nomes de campo sao os MESMOS nos dois
documentos (ver observation_run.py/ct_listener.py::_flush_batch/
orchestrator.py::investigate_domain) -- nao uma segunda convencao.

Sem esta etapa o run acontece e nao sobra nenhum numero verificavel para o
FINDINGS.md nem para o video.

Duas saidas do MESMO dado:
  --format text (default) -- para aparecer no terminal durante o video.
  --format markdown -- bloco pronto para colar em FINDINGS.md.

Uso:
    python observation_report.py --run-id obs-2026-08-28
    python observation_report.py --run-id obs-2026-08-28 --format markdown > findings_run.md
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from google.cloud import firestore

import metrics_report
from config import settings

_TOP_N = 10

_BOLD = "\033[1m"
_RESET = "\033[0m"


def _bold(text: str) -> str:
    # Mesmo criterio de metrics_report.py (isatty) -- funcao PROPRIA, nao
    # importada de la: reusar `compute_report`/`compute_funnel`/
    # `render_report` (API publica, matematica testada) faz sentido;
    # alcancar `metrics_report._bold` (privado, prefixo `_`) para uma
    # cosmetica de terminal nao faria.
    return f"{_BOLD}{text}{_RESET}" if sys.stdout.isatty() else text


_db: firestore.Client | None = None


def _get_db() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client()
    return _db


def fetch_run_totals(run_id: str) -> dict[str, Any]:
    doc = _get_db().collection(settings.observation_runs_collection).document(run_id).get()
    return doc.to_dict() or {} if doc.exists else {}


def fetch_checkpoints(run_id: str) -> list[dict[str, Any]]:
    """Serie temporal (item 3): um documento imutavel por checkpoint,
    gravado por `observation_run.record_checkpoint`. Ordenada por
    `checkpoint_at` -- e o que da a evolucao dos contadores ao longo das
    48h, nao so o total final."""
    docs = (
        _get_db()
        .collection(settings.observation_runs_collection)
        .document(run_id)
        .collection("checkpoints")
        .order_by("checkpoint_at")
        .stream()
    )
    return [d.to_dict() or {} for d in docs]


def fetch_investigations_since(started_at: Any) -> list[dict[str, Any]]:
    """Todos os dossies com `investigated_at >= started_at` -- a JANELA do
    run (`started_at` gravado uma unica vez por
    `observation_run._ensure_started_at`, resistente a re-disparo do
    Scheduler). Devolve lista vazia (nao erro) se `started_at` for `None`
    (run sem nenhum `bump()` gravado ainda)."""
    if started_at is None:
        return []
    query = _get_db().collection(settings.firestore_collection).where("investigated_at", ">=", started_at)
    return [d.to_dict() or {} for d in query.stream()]


def compute_top_brands(investigations: list[dict[str, Any]], top_n: int = _TOP_N) -> list[tuple[str, int]]:
    counter = Counter(inv.get("matched_brand") for inv in investigations if inv.get("matched_brand"))
    return counter.most_common(top_n)


def compute_top_registrars_and_asns(
    investigations: list[dict[str, Any]], top_n: int = _TOP_N
) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """So dossies com bundle de evidencia (`evidence_agent.py`, tipicamente
    so os MALICIOUS -- ver CLAUDE.md) tem `evidence.rdap`/`evidence.hosting`.
    Registrador/ASN vem de RDAP/DNS reais, mas e dado influenciado pelo
    proprio atacante (quem registrou o dominio de phishing) -- contado
    aqui so como sinal agregado de infraestrutura para o relatorio, nunca
    usado para nenhuma decisao automatica."""
    registrars: Counter[str] = Counter()
    asns: Counter[str] = Counter()
    for inv in investigations:
        evidence = inv.get("evidence") or {}
        registrar = (evidence.get("rdap") or {}).get("registrar")
        if registrar:
            registrars[registrar] += 1
        asn_org = (evidence.get("hosting") or {}).get("asn_org")
        if asn_org:
            asns[asn_org] += 1
    return registrars.most_common(top_n), asns.most_common(top_n)


def compute_average_latency_seconds(investigations: list[dict[str, Any]]) -> float | None:
    """Media de `investigation_latency_seconds` (Etapa C -- ver
    orchestrator.py::_save_investigation) sobre os dossies da janela que
    tem o campo gravado. `None` (nao 0.0) quando nenhum dossie tem o
    campo -- nunca fingir uma media de amostra vazia."""
    values = [
        inv["investigation_latency_seconds"]
        for inv in investigations
        if inv.get("investigation_latency_seconds") is not None
    ]
    return statistics.mean(values) if values else None


def compute_websocket_coverage(totals: dict[str, Any]) -> dict[str, float | int]:
    """Lacunas de cobertura do websocket certstream (item 4) -- limitacao
    HONESTA de cobertura, nao escondida: certstream nao tem replay, entao
    todo segundo de lacuna e evento potencialmente perdido para sempre."""
    disconnects = int(totals.get("websocket_disconnects_total", 0) or 0)
    gap_total = float(totals.get("websocket_gap_seconds_total", 0.0) or 0.0)
    avg_gap = gap_total / disconnects if disconnects else 0.0
    return {
        "disconnects_total": disconnects,
        "gap_seconds_total": gap_total,
        "avg_gap_seconds": avg_gap,
    }


@dataclass
class ObservationReport:
    run_id: str
    totals: dict[str, Any] = field(default_factory=dict)
    cascade: dict[str, Any] = field(default_factory=dict)
    funnel: list[dict] = field(default_factory=list)
    confirmed_malicious: int = 0
    top_brands: list[tuple[str, int]] = field(default_factory=list)
    top_registrars: list[tuple[str, int]] = field(default_factory=list)
    top_asns: list[tuple[str, int]] = field(default_factory=list)
    avg_latency_seconds: float | None = None
    websocket_coverage: dict[str, float | int] = field(default_factory=dict)
    checkpoints: list[dict[str, Any]] = field(default_factory=list)


def build_report(run_id: str) -> ObservationReport:
    totals = fetch_run_totals(run_id)
    if not totals:
        return ObservationReport(run_id=run_id)

    investigations = fetch_investigations_since(totals.get("started_at"))
    cascade = metrics_report.compute_report(totals)
    confirmed_malicious = int(totals.get("malicious_confirmed_total", 0) or 0)
    funnel = metrics_report.compute_funnel(cascade, confirmed_malicious)
    top_registrars, top_asns = compute_top_registrars_and_asns(investigations)

    return ObservationReport(
        run_id=run_id,
        totals=totals,
        cascade=cascade,
        funnel=funnel,
        confirmed_malicious=confirmed_malicious,
        top_brands=compute_top_brands(investigations),
        top_registrars=top_registrars,
        top_asns=top_asns,
        avg_latency_seconds=compute_average_latency_seconds(investigations),
        websocket_coverage=compute_websocket_coverage(totals),
        checkpoints=fetch_checkpoints(run_id),
    )


def _fmt_seconds(value: float | None) -> str:
    if value is None:
        return "N/A (nenhum dossie da janela tem detected_at registrado)"
    if value < 120:
        return f"{value:.1f}s"
    return f"{value / 60:.1f}min"


def render_text(report: ObservationReport) -> str:
    if not report.totals:
        return (
            f"Nenhum dado encontrado em "
            f"'{settings.observation_runs_collection}/{report.run_id}'. "
            "O run ainda nao gravou nenhum bump(), ou OBSERVATION_RUN_ID nao "
            "bate com o run que voce quer relatar."
        )

    lines = [metrics_report.render_report(report.cascade, report.funnel)]
    lines.append("")
    lines.append(_bold(f"Run de observacao: {report.run_id}"))
    lines.append(f"  Iniciado em ........................ {report.totals.get('started_at')}")
    lines.append(f"  Ultima atualizacao ................. {report.totals.get('last_updated_at')}")
    lines.append(f"  Tempo medio certificado->dossie ..... {_fmt_seconds(report.avg_latency_seconds)}")
    lines.append("")

    lines.append(_bold("Cobertura do websocket certstream (limitacao honesta)"))
    cov = report.websocket_coverage
    lines.append(f"  Desconexoes ........................ {cov.get('disconnects_total', 0):>12,}")
    lines.append(f"  Lacuna total sem cobertura ......... {_fmt_seconds(cov.get('gap_seconds_total'))}")
    lines.append(f"  Lacuna media por desconexao ........ {_fmt_seconds(cov.get('avg_gap_seconds'))}")
    lines.append("")

    lines.append(_bold(f"Top {_TOP_N} marcas visadas (dossies da janela do run)"))
    if report.top_brands:
        for brand, count in report.top_brands:
            lines.append(f"  {brand:<30} {count:>6,}")
    else:
        lines.append("  (nenhum dossie com matched_brand na janela do run)")
    lines.append("")

    lines.append(_bold(f"Top {_TOP_N} registrars (dossies MALICIOUS com evidencia coletada)"))
    if report.top_registrars:
        for registrar, count in report.top_registrars:
            lines.append(f"  {registrar:<30} {count:>6,}")
    else:
        lines.append("  (nenhum dossie da janela tem evidence.rdap.registrar)")
    lines.append("")

    lines.append(_bold(f"Top {_TOP_N} ASNs/organizacoes de hospedagem"))
    if report.top_asns:
        for asn_org, count in report.top_asns:
            lines.append(f"  {asn_org:<30} {count:>6,}")
    else:
        lines.append("  (nenhum dossie da janela tem evidence.hosting.asn_org)")
    lines.append("")
    lines.append(_bold(f"Checkpoints gravados: {len(report.checkpoints)}"))

    return "\n".join(lines)


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "_(sem dados)_\n"
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(out) + "\n"


def render_markdown(report: ObservationReport) -> str:
    if not report.totals:
        return f"## Run de observacao `{report.run_id}`\n\nNenhum dado encontrado.\n"

    c = report.cascade
    md = [f"## Run de observacao `{report.run_id}`", ""]
    md.append(f"- Iniciado em: `{report.totals.get('started_at')}`")
    md.append(f"- Ultima atualizacao: `{report.totals.get('last_updated_at')}`")
    md.append(f"- Tempo medio certificado -> dossie: **{_fmt_seconds(report.avg_latency_seconds)}**")
    md.append("")

    md.append("### Funil (prefiltro -> Gemma -> Gemini)")
    md.append("")
    md.append(
        _md_table(
            ["Etapa", "Contagem", "% do topo", "Custo acumulado (USD)"],
            [
                [s["label"], f"{s['count']:,}", f"{s['pct_of_top']:.2f}%", f"${s['cumulative_cost']:.4f}"]
                for s in report.funnel
            ],
        )
    )

    md.append("### Custo")
    md.append("")
    md.append(f"- Custo real (Gemini): **${c['cost_real']:.4f}**")
    md.append(f"- Custo do Gemma (CPU self-hosted): **${c['gemma_cost']:.4f}**")
    md.append(
        f"- Custo hipotetico SEM a cascata (tudo direto no Gemini): "
        f"**${c['hypothetical_cost_no_prefilter']:.4f}**"
    )
    total_saved = c["cost_saved_by_prefilter"] + c["cost_saved_by_gemma"]
    md.append(f"- Economia total gerada pela cascata: **${total_saved:.4f}**")
    md.append("")

    md.append("### Cobertura do websocket certstream (limitacao honesta de cobertura)")
    md.append("")
    cov = report.websocket_coverage
    md.append(f"- Desconexoes: {cov.get('disconnects_total', 0):,}")
    md.append(f"- Lacuna total sem cobertura: {_fmt_seconds(cov.get('gap_seconds_total'))}")
    md.append(f"- Lacuna media por desconexao: {_fmt_seconds(cov.get('avg_gap_seconds'))}")
    md.append(
        "- certstream nao tem replay: todo evento emitido durante uma lacuna foi perdido "
        "permanentemente, nao apenas atrasado."
    )
    md.append("")

    md.append(f"### Top {_TOP_N} marcas visadas")
    md.append("")
    md.append(_md_table(["Marca", "Dossies"], [[b, n] for b, n in report.top_brands]))

    md.append(f"### Top {_TOP_N} registrars")
    md.append("")
    md.append(_md_table(["Registrar", "Dominios"], [[r, n] for r, n in report.top_registrars]))

    md.append(f"### Top {_TOP_N} ASNs / organizacoes de hospedagem")
    md.append("")
    md.append(_md_table(["ASN/Org", "Dominios"], [[a, n] for a, n in report.top_asns]))

    md.append(f"### Checkpoints (serie temporal, {len(report.checkpoints)} pontos)")
    md.append("")
    md.append(
        _md_table(
            ["checkpoint_at", "ingeridos", "descartados prefiltro", "enviados ao Gemini", "MALICIOUS"],
            [
                [
                    cp.get("checkpoint_at"),
                    cp.get("certificates_ingested_total", 0),
                    cp.get("certificates_discarded_by_prefilter_total", 0),
                    cp.get("llm_invocations_total", 0),
                    cp.get("malicious_confirmed_total", 0),
                ]
                for cp in report.checkpoints
            ],
        )
    )

    return "\n".join(md)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-id",
        default=settings.observation_run_id,
        help="ID do run de observacao (default: settings.observation_run_id / OBSERVATION_RUN_ID).",
    )
    parser.add_argument("--format", choices=["text", "markdown"], default="text")
    args = parser.parse_args()

    if not args.run_id:
        print(
            "Nenhum --run-id passado e OBSERVATION_RUN_ID nao esta configurado. "
            "Passe --run-id explicitamente.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    report = build_report(args.run_id)
    if args.format == "markdown":
        print(render_markdown(report))
    else:
        print(render_text(report))


if __name__ == "__main__":
    main()
