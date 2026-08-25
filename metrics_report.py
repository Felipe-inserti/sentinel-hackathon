#!/usr/bin/env python3
"""Relatorio de custo/economia do Sentinel -- le o documento compartilhado
`metrics/pipeline_totals` no Firestore (alimentado por `ct_listener.py` e
`orchestrator.py` via `telemetry.py`), consulta a colecao de investigacoes
para contar confirmados maliciosos, e imprime a cascata completa em funil:

    certificados ingeridos
      -> sobreviventes do prefiltro (matematica, custo zero)
        -> sobreviventes da triagem Gemma (CPU self-hosted, quase zero)
          -> investigados pelo Gemini (scraping + LLM, caro)
            -> confirmados maliciosos

Com percentual (relativo ao topo do funil) e custo acumulado em cada
degrau -- este e o slide central da apresentacao.

Uso:
    python metrics_report.py
"""

from __future__ import annotations

import sys

from google.cloud import firestore

from config import settings

_METRICS_DOCUMENT = "pipeline_totals"
_FUNNEL_BAR_WIDTH = 40

_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"
_GREEN = "\033[32m"
_CYAN = "\033[36m"


def _bold(text: str) -> str:
    return f"{_BOLD}{text}{_RESET}" if sys.stdout.isatty() else text


def _green(text: str) -> str:
    return f"{_GREEN}{_BOLD}{text}{_RESET}" if sys.stdout.isatty() else text


def _cyan(text: str) -> str:
    return f"{_CYAN}{text}{_RESET}" if sys.stdout.isatty() else text


def _dim(text: str) -> str:
    return f"{_DIM}{text}{_RESET}" if sys.stdout.isatty() else text


def fetch_totals() -> dict[str, float | int]:
    db = firestore.Client()
    doc = db.collection(settings.metrics_firestore_collection).document(_METRICS_DOCUMENT).get()
    if not doc.exists:
        return {}
    return doc.to_dict() or {}


def fetch_confirmed_malicious_count() -> int:
    """Conta documentos com `classification == "MALICIOUS"` na colecao de
    investigacoes -- e o ultimo degrau do funil, e nao vem do documento de
    contadores (essa contagem nao e um metric OTel, e uma consulta
    agregada direto na fonte, ja que o valor e derivado do que ja esta
    persistido em cada investigacao)."""
    db = firestore.Client()
    query = db.collection(settings.firestore_collection).where("classification", "==", "MALICIOUS")
    result = query.count().get()
    return int(result[0][0].value)


def compute_report(totals: dict[str, float | int]) -> dict[str, float | int]:
    ingested = totals.get("certificates_ingested_total", 0)
    discarded = totals.get("certificates_discarded_by_prefilter_total", 0)
    llm_invocations = totals.get("llm_invocations_total", 0)
    cache_hits = totals.get("cache_hits_total", 0)
    tokens_consumed = totals.get("tokens_consumed_total", 0)
    cost_real = totals.get("estimated_cost_usd_total", 0.0)

    gemma_triage_total = totals.get("gemma_triage_total", 0)
    gemma_discarded = totals.get("gemma_discarded_total", 0)
    gemma_escalated = totals.get("gemma_escalated_total", 0)
    gemma_fallback = totals.get("gemma_fallback_total", 0)
    gemma_cost = totals.get("gemma_triage_cost_usd_total", 0.0)

    discard_rate = (discarded / ingested * 100) if ingested else 0.0
    total_investigations = llm_invocations + cache_hits
    cache_hit_rate = (cache_hits / total_investigations * 100) if total_investigations else 0.0

    avg_cost_per_llm_call = (cost_real / llm_invocations) if llm_invocations else 0.0
    avg_cost_per_investigation = (cost_real / total_investigations) if total_investigations else 0.0

    gemma_discard_rate = (gemma_discarded / gemma_triage_total * 100) if gemma_triage_total else 0.0

    # Os dois numeros do pitch: quanto teria sido gasto se cada camada nao
    # existisse, usando o custo medio real observado por chamada de LLM
    # (nao um chute -- vem de tokens/preco reais ja medidos).
    hypothetical_cost_no_prefilter = ingested * avg_cost_per_llm_call
    cost_saved_by_prefilter = discarded * avg_cost_per_llm_call
    cost_saved_by_gemma = gemma_discarded * avg_cost_per_llm_call

    return {
        "ingested": ingested,
        "discarded": discarded,
        "discard_rate": discard_rate,
        "llm_invocations": llm_invocations,
        "cache_hits": cache_hits,
        "total_investigations": total_investigations,
        "cache_hit_rate": cache_hit_rate,
        "tokens_consumed": tokens_consumed,
        "cost_real": cost_real,
        "avg_cost_per_llm_call": avg_cost_per_llm_call,
        "avg_cost_per_investigation": avg_cost_per_investigation,
        "hypothetical_cost_no_prefilter": hypothetical_cost_no_prefilter,
        "cost_saved_by_prefilter": cost_saved_by_prefilter,
        "gemma_triage_total": gemma_triage_total,
        "gemma_discarded": gemma_discarded,
        "gemma_escalated": gemma_escalated,
        "gemma_fallback": gemma_fallback,
        "gemma_cost": gemma_cost,
        "gemma_discard_rate": gemma_discard_rate,
        "cost_saved_by_gemma": cost_saved_by_gemma,
    }


def compute_funnel(r: dict[str, float | int], confirmed_malicious: int) -> list[dict]:
    ingested = r["ingested"]
    prefilter_survivors = ingested - r["discarded"]
    gemma_survivors = r["gemma_triage_total"] - r["gemma_discarded"]
    gemini_investigated = r["total_investigations"]

    cost_after_prefilter = 0.0  # prefiltro e matematica pura, custo zero
    cost_after_gemma = r["gemma_cost"]
    cost_after_gemini = r["gemma_cost"] + r["cost_real"]

    steps = [
        ("Certificados ingeridos", ingested, 0.0),
        ("Sobreviventes do prefiltro", prefilter_survivors, cost_after_prefilter),
        ("Sobreviventes da triagem Gemma", gemma_survivors, cost_after_gemma),
        ("Investigados pelo Gemini", gemini_investigated, cost_after_gemini),
        ("Confirmados maliciosos", confirmed_malicious, cost_after_gemini),
    ]

    funnel = []
    base = ingested if ingested else 1
    for label, count, cumulative_cost in steps:
        pct_of_top = count / base * 100
        funnel.append(
            {"label": label, "count": count, "pct_of_top": pct_of_top, "cumulative_cost": cumulative_cost}
        )
    return funnel


def _render_funnel_bar(pct_of_top: float) -> str:
    filled = round(_FUNNEL_BAR_WIDTH * min(pct_of_top, 100.0) / 100.0)
    return "#" * filled + "." * (_FUNNEL_BAR_WIDTH - filled)


def render_funnel(funnel: list[dict]) -> str:
    lines = [_bold("Funil de custo -- cascata de tres niveis (prefiltro -> Gemma -> Gemini)")]
    lines.append("")
    for step in funnel:
        bar = _render_funnel_bar(step["pct_of_top"])
        lines.append(
            f"  {step['label']:<32} {_cyan(bar)} "
            f"{step['count']:>9,} ({step['pct_of_top']:>6.2f}%)  "
            f"${step['cumulative_cost']:.4f}"
        )
    lines.append("")
    return "\n".join(lines)


def render_report(r: dict[str, float | int], funnel: list[dict]) -> str:
    lines = []
    lines.append(_bold("=" * 68))
    lines.append(_bold("  SENTINEL -- Relatorio de Token Economy (cascata de 3 niveis)"))
    lines.append(_bold("=" * 68))
    lines.append("")
    lines.append(render_funnel(funnel))
    lines.append(_bold("Ingestao (Plano 1 -- Certificate Transparency)"))
    lines.append(f"  Certificados ingeridos ........... {r['ingested']:>12,}")
    lines.append(f"  Descartados pelo prefiltro ........ {r['discarded']:>12,}")
    lines.append(f"  Taxa de descarte do prefiltro ..... {r['discard_rate']:>11.2f}%")
    lines.append("")
    lines.append(_bold("Triagem Gemma (camada intermediaria -- ver gemma_triage.py)"))
    lines.append(f"  Dominios triados .................. {r['gemma_triage_total']:>12,}")
    lines.append(f"  Descartados pelo Gemma ............. {r['gemma_discarded']:>12,}")
    lines.append(f"  Taxa de descarte do Gemma .......... {r['gemma_discard_rate']:>11.2f}%")
    lines.append(f"  Escalados (ESCALATE_IMMEDIATE) ..... {r['gemma_escalated']:>12,}")
    lines.append(f"  Fallbacks (fail-open) .............. {r['gemma_fallback']:>12,}")
    lines.append(f"  Custo do Gemma (CPU, self-hosted) .. ${r['gemma_cost']:>10.4f}")
    lines.append("")
    lines.append(_bold("Investigacao (Plano 2 -- Cache + Gemini)"))
    lines.append(f"  Total de investigacoes ............. {r['total_investigations']:>12,}")
    lines.append(f"  Cache hits ......................... {r['cache_hits']:>12,}")
    lines.append(f"  Chamadas reais ao Gemini ........... {r['llm_invocations']:>12,}")
    lines.append(f"  Taxa de cache hit .................. {r['cache_hit_rate']:>11.2f}%")
    lines.append(f"  Tokens consumidos (total) .......... {r['tokens_consumed']:>12,}")
    lines.append("")
    lines.append(_bold("Custo"))
    lines.append(f"  Custo real -- Gemini (USD) ......... ${r['cost_real']:>10.4f}")
    lines.append(f"  Custo medio / chamada ao Gemini ..... ${r['avg_cost_per_llm_call']:>10.6f}")
    lines.append(f"  Custo medio / investigacao .......... ${r['avg_cost_per_investigation']:>10.6f}")
    lines.append("")
    lines.append(_bold("Os dois numeros do pitch"))
    lines.append(
        f"  Custo hipotetico SEM nenhum filtro .. ${r['hypothetical_cost_no_prefilter']:>10.4f}"
        + _dim("  (se todo ingerido fosse direto ao Gemini)")
    )
    lines.append(_green(f"  Economia gerada pelo prefiltro ....... ${r['cost_saved_by_prefilter']:>10.4f}"))
    lines.append(_green(f"  Economia gerada pelo Gemma (extra) ... ${r['cost_saved_by_gemma']:>10.4f}"))
    total_saved = r["cost_saved_by_prefilter"] + r["cost_saved_by_gemma"]
    lines.append(_green(f"  Economia total .......................  ${total_saved:>10.4f}"))
    if r["hypothetical_cost_no_prefilter"] > 0:
        reduction_pct = total_saved / r["hypothetical_cost_no_prefilter"] * 100
        lines.append(_green(f"  Reducao de custo total ................ {reduction_pct:>9.2f}%"))
    lines.append("")
    lines.append(_bold("=" * 68))
    return "\n".join(lines)


def main() -> None:
    totals = fetch_totals()
    if not totals:
        print(
            "Nenhuma metrica encontrada ainda em "
            f"'{settings.metrics_firestore_collection}/{_METRICS_DOCUMENT}'. "
            "Rode ct_listener.py e orchestrator.py primeiro."
        )
        return

    try:
        confirmed_malicious = fetch_confirmed_malicious_count()
    except Exception as exc:
        print(f"Aviso: nao consegui contar confirmados maliciosos no Firestore ({exc}). Usando 0.")
        confirmed_malicious = 0

    report = compute_report(totals)
    funnel = compute_funnel(report, confirmed_malicious)
    print(render_report(report, funnel))


if __name__ == "__main__":
    main()
