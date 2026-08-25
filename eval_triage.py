#!/usr/bin/env python3
"""Avaliacao da camada de triagem Gemma contra um conjunto rotulado.

Mede precisao, recall e, principalmente, FALSOS NEGATIVOS -- um DISCARD
errado (Gemma descarta um dominio que e phishing de verdade) e o unico
erro caro desta camada (requisito 12: calibrar priorizando recall sobre
precisao). Compara tambem com o Gemini nos mesmos sinais estruturados
(sem scraping) quando ha credenciais Vertex disponiveis -- requisito 11.

Uso:
    python eval_triage.py                 # so Gemma (precisa de Ollama rodando)
    python eval_triage.py --with-gemini    # tambem chama o Gemini para comparar

Os resultados numericos deste script (rodado contra um Gemma real) viram
conteudo de FINDINGS.md.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field

from gemma_triage import DomainSignals, TriageResult, triage_batch


@dataclass(frozen=True)
class LabeledCase:
    signals: DomainSignals
    is_actually_malicious: bool  # rotulo humano (ground truth)
    notes: str = ""


# Conjunto rotulado: typosquatting brasileiro (padroes reais, dominios
# sinteticos -- nao apontamos para infraestrutura maliciosa ativa),
# dominios legitimos com similaridade coincidente (para medir falso
# positivo), e casos-limite para calibracao.
LABELED_DATASET: list[LabeledCase] = [
    # --- Maliciosos: recall aqui e o que importa -----------------------
    LabeledCase(
        DomainSignals(domain="nub4nk-suporte-oficial.xyz", target_brand="nubank", similarity_score=0.95,
                      heuristics_triggered=["leetspeak"], domain_tokens=["suporte", "oficial"],
                      tld="xyz", certificate_age_seconds=180),
        True, "leetspeak + urgencia extrema (cert com 3min)",
    ),
    LabeledCase(
        DomainSignals(domain="loggi-entregas-rastreio-pacote.com", target_brand="loggi", similarity_score=0.86,
                      heuristics_triggered=["sliding_window"], domain_tokens=["entregas", "rastreio", "pacote"],
                      tld="com", certificate_age_seconds=3600),
        True, "termos de rastreio classicos de phishing de logistica",
    ),
    LabeledCase(
        DomainSignals(domain="ifood-cupom-gratis-hoje.com", target_brand="ifood", similarity_score=0.79,
                      heuristics_triggered=["sliding_window"], domain_tokens=["cupom", "gratis", "hoje"],
                      tld="com", certificate_age_seconds=900),
        True, "isca de cupom + urgencia (\"hoje\") + cert recente",
    ),
    LabeledCase(
        DomainSignals(domain="nubank-seguranca-conta.tk", target_brand="nubank", similarity_score=0.91,
                      heuristics_triggered=[], domain_tokens=["seguranca", "conta"],
                      tld="tk", certificate_age_seconds=60),
        True, "TLD gratuito de alto abuso + isca de seguranca + cert com 1min",
    ),
    LabeledCase(
        DomainSignals(domain="app-nubank-atualizacao.xyz", target_brand="nubank", similarity_score=0.88,
                      heuristics_triggered=[], domain_tokens=["app", "atualizacao"],
                      tld="xyz", certificate_age_seconds=420),
        True, "isca de atualizacao de app bancario",
    ),
    LabeledCase(
        DomainSignals(domain="seguro-nubank-cliente.com", target_brand="nubank", similarity_score=0.83,
                      heuristics_triggered=[], domain_tokens=["seguro", "cliente"],
                      tld="com", certificate_age_seconds=1800),
        True, "\"seguro\" no proprio dominio -- tenta preemptivamente parecer confiavel",
    ),
    LabeledCase(
        DomainSignals(domain="nu-bank-oficial-app.com", target_brand="nubank", similarity_score=0.90,
                      heuristics_triggered=[], domain_tokens=["oficial", "app"],
                      tld="com", certificate_age_seconds=7200),
        True, "hifen separando a marca + \"oficial\" (classico)",
    ),
    LabeledCase(
        DomainSignals(domain="loggi-rastreamento-encomenda.online", target_brand="loggi", similarity_score=0.81,
                      heuristics_triggered=["sliding_window"], domain_tokens=["rastreamento", "encomenda"],
                      tld="online", certificate_age_seconds=300),
        True, "TLD .online + cert com 5min -- ataque muito recente",
    ),
    LabeledCase(
        DomainSignals(domain="ifood-parceiro-cadastro.xyz", target_brand="ifood", similarity_score=0.77,
                      heuristics_triggered=[], domain_tokens=["parceiro", "cadastro"],
                      tld="xyz", certificate_age_seconds=600),
        True, "isca de cadastro de parceiro/restaurante",
    ),
    LabeledCase(
        DomainSignals(domain="nubank-clientes-2fa.com", target_brand="nubank", similarity_score=0.85,
                      heuristics_triggered=[], domain_tokens=["clientes", "2fa"],
                      tld="com", certificate_age_seconds=240),
        True, "isca de autenticacao 2FA -- alto valor para o atacante",
    ),
    # --- Legitimos: falso positivo aqui e caro em volume, mas aceitavel -
    LabeledCase(
        DomainSignals(domain="loggishop-embalagens.com.br", target_brand="loggi", similarity_score=0.62,
                      heuristics_triggered=["sliding_window"], domain_tokens=["embalagens"],
                      tld="com.br", certificate_age_seconds=63072000),
        False, "negocio de embalagens com nome coincidente, cert de 2 anos",
    ),
    LabeledCase(
        DomainSignals(domain="ifoodtech-consultoria.com.br", target_brand="ifood", similarity_score=0.58,
                      heuristics_triggered=[], domain_tokens=["tech", "consultoria"],
                      tld="com.br", certificate_age_seconds=31536000),
        False, "consultoria de TI com nome coincidente, cert de 1 ano",
    ),
    LabeledCase(
        DomainSignals(domain="nubanco-investimentos.com.br", target_brand="nubank", similarity_score=0.55,
                      heuristics_triggered=[], domain_tokens=["investimentos"],
                      tld="com.br", certificate_age_seconds=94608000),
        False, "assessoria financeira com nome coincidente, cert de 3 anos, TLD formal",
    ),
    LabeledCase(
        DomainSignals(domain="loggibike-fretes.com.br", target_brand="loggi", similarity_score=0.60,
                      heuristics_triggered=[], domain_tokens=["fretes"],
                      tld="com.br", certificate_age_seconds=15768000),
        False, "transportadora de bicicleta com nome coincidente, meio ano de idade",
    ),
    # --- Casos-limite: calibracao de threshold --------------------------
    LabeledCase(
        DomainSignals(domain="nubank-promocoes.xyz", target_brand="nubank", similarity_score=0.80,
                      heuristics_triggered=[], domain_tokens=["promocoes"],
                      tld="xyz", certificate_age_seconds=172800),
        True, "limite: similaridade moderada, TLD suspeito, mas 2 dias de idade (nem tao urgente)",
    ),
    LabeledCase(
        DomainSignals(domain="ifood-parceiros-oficial.com.br", target_brand="ifood", similarity_score=0.70,
                      heuristics_triggered=[], domain_tokens=["parceiros", "oficial"],
                      tld="com.br", certificate_age_seconds=2592000),
        True, "limite: TLD formal e 1 mes de idade, mas \"oficial\" + marca e isca classica",
    ),
    LabeledCase(
        DomainSignals(domain="loggi-carreiras.com", target_brand="loggi", similarity_score=0.65,
                      heuristics_triggered=[], domain_tokens=["carreiras"],
                      tld="com", certificate_age_seconds=47304000),
        False, "limite: pode ser pagina de vagas legitima de terceiro, cert de 1.5 ano",
    ),
]


def compute_confusion(
    dataset: list[LabeledCase], predictions: dict[str, TriageResult]
) -> dict[str, float | int]:
    tp = fp = tn = fn = 0
    false_negatives: list[str] = []

    for case in dataset:
        result = predictions.get(case.signals.domain)
        predicted_discard = result is None or result.verdict == "DISCARD"
        actual_malicious = case.is_actually_malicious

        if actual_malicious and not predicted_discard:
            tp += 1
        elif actual_malicious and predicted_discard:
            fn += 1
            false_negatives.append(case.signals.domain)
        elif not actual_malicious and predicted_discard:
            tn += 1
        else:
            fp += 1

    total = len(dataset)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    false_negative_rate = fn / (tp + fn) if (tp + fn) else 0.0
    accuracy = (tp + tn) / total if total else 0.0

    return {
        "total": total, "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "precision": precision, "recall": recall,
        "false_negative_rate": false_negative_rate, "accuracy": accuracy,
        "false_negatives": false_negatives,
    }


async def run_gemma_eval() -> tuple[dict, dict[str, TriageResult], float]:
    """Roda em lotes de `settings.gemma_batch_max_size` -- mesmo tamanho
    que `ct_listener.py` usa em producao -- para o resultado refletir o
    comportamento real do sistema, nao uma chamada gigante artificial que
    so dispararia fail-open."""
    from config import settings

    signals = [case.signals for case in LABELED_DATASET]
    combined_results: dict[str, TriageResult] = {}
    total_cost = 0.0
    any_fallback = False

    start = time.monotonic()
    for i in range(0, len(signals), settings.gemma_batch_max_size):
        chunk = signals[i : i + settings.gemma_batch_max_size]
        outcome = await triage_batch(chunk)
        combined_results.update(outcome.results)
        total_cost += outcome.cost_usd
        any_fallback = any_fallback or outcome.fallback_used
    elapsed_ms = (time.monotonic() - start) * 1000

    metrics = compute_confusion(LABELED_DATASET, combined_results)
    metrics["fallback_used"] = any_fallback
    metrics["cost_usd"] = total_cost
    metrics["latency_ms_total"] = elapsed_ms
    return metrics, combined_results, elapsed_ms


GEMINI_SIGNAL_PROMPT_TEMPLATE = """Voce recebe sinais estruturados (nao o conteudo da pagina) de um \
dominio suspeito e deve classifica-lo como MALICIOUS ou SAFE.

domain={domain} target_brand={target_brand} similarity_score={similarity_score} \
heuristics_triggered={heuristics_triggered} domain_tokens={domain_tokens} tld={tld} \
certificate_age_seconds={certificate_age_seconds}"""


async def run_gemini_eval() -> tuple[dict, float] | None:
    """Compara com o Gemini nos MESMOS sinais estruturados (sem scraping,
    para ser uma comparacao justa de custo/latencia por decisao). Exige
    credenciais Vertex reais -- se nao houver, devolve None e o relatorio
    reporta que a comparacao foi pulada, em vez de fingir um numero."""
    try:
        from llm_client import llm_client
        from plane2_agents.orchestrator import AnalysisResult
    except Exception as exc:
        print(f"Aviso: nao consegui importar o cliente do Gemini ({exc}). Pulando comparacao.")
        return None

    start = time.monotonic()
    tp = fp = tn = fn = 0
    total_cost = 0.0
    try:
        for case in LABELED_DATASET:
            s = case.signals
            prompt = GEMINI_SIGNAL_PROMPT_TEMPLATE.format(
                domain=s.domain, target_brand=s.target_brand, similarity_score=s.similarity_score,
                heuristics_triggered=s.heuristics_triggered, domain_tokens=s.domain_tokens,
                tld=s.tld, certificate_age_seconds=s.certificate_age_seconds,
            )
            result = await llm_client.generate(
                system_prompt="Classifique o dominio a seguir. Responda so com o schema pedido.",
                untrusted_data=prompt,
                response_schema=AnalysisResult,
            )
            predicted_malicious = result.data.classification == "MALICIOUS"
            if case.is_actually_malicious and predicted_malicious:
                tp += 1
            elif case.is_actually_malicious and not predicted_malicious:
                fn += 1
            elif not case.is_actually_malicious and not predicted_malicious:
                tn += 1
            else:
                fp += 1
    except Exception as exc:
        print(f"Aviso: chamada ao Gemini falhou ({exc}). Pulando comparacao -- provavelmente sem credenciais Vertex reais neste ambiente.")
        return None

    elapsed_ms = (time.monotonic() - start) * 1000
    total = len(LABELED_DATASET)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    return {
        "total": total, "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "precision": precision, "recall": recall,
        "false_negative_rate": fn / (tp + fn) if (tp + fn) else 0.0,
        "accuracy": (tp + tn) / total if total else 0.0,
    }, elapsed_ms


def render_metrics(label: str, m: dict) -> str:
    lines = [f"\n=== {label} ===", f"  Total de casos avaliados: {m['total']}"]
    lines.append(f"  TP={m['tp']} FP={m['fp']} TN={m['tn']} FN={m['fn']}")
    lines.append(f"  Precisao:  {m['precision']*100:.1f}%")
    lines.append(f"  Recall:    {m['recall']*100:.1f}%")
    lines.append(f"  Acuracia:  {m['accuracy']*100:.1f}%")
    lines.append(f"  Taxa de FALSO NEGATIVO: {m['false_negative_rate']*100:.1f}%  <-- o erro caro desta camada")
    if m.get("false_negatives"):
        lines.append(f"  Dominios com falso negativo: {m['false_negatives']}")
    if "cost_usd" in m:
        lines.append(f"  Custo total: ${m['cost_usd']:.6f}")
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-gemini", action="store_true", help="Tambem compara com o Gemini")
    args = parser.parse_args()

    print(f"Avaliando {len(LABELED_DATASET)} casos rotulados contra o Gemma...")
    try:
        gemma_metrics, _, gemma_latency_ms = await run_gemma_eval()
    except Exception as exc:
        print(f"ERRO: nao consegui rodar a avaliacao do Gemma ({exc}).")
        print("Verifique se o Ollama esta rodando e GEMMA_OLLAMA_BASE_URL aponta pra ele.")
        sys.exit(1)

    print(render_metrics("GEMMA 3 270M (triagem)", gemma_metrics))
    print(f"  Latencia total do lote: {gemma_latency_ms:.0f}ms para {len(LABELED_DATASET)} dominios "
          f"({gemma_latency_ms/len(LABELED_DATASET):.0f}ms/dominio em media)")

    if args.with_gemini:
        print("\nAvaliando os mesmos casos contra o Gemini (mesmos sinais, sem scraping)...")
        gemini_result = await run_gemini_eval()
        if gemini_result is not None:
            gemini_metrics, gemini_latency_ms = gemini_result
            print(render_metrics("Gemini (mesma tarefa, para comparacao)", gemini_metrics))
            print(f"  Latencia total: {gemini_latency_ms:.0f}ms "
                  f"({gemini_latency_ms/len(LABELED_DATASET):.0f}ms/dominio em media)")
            print(f"\n  Gemma e {gemini_latency_ms/max(gemma_latency_ms,1):.1f}x mais rapido que o Gemini nesta tarefa")
        else:
            print("Comparacao com o Gemini pulada (ver aviso acima).")


if __name__ == "__main__":
    asyncio.run(main())
