#!/usr/bin/env python3
"""Replay de investigacao -- demonstracao viva da memoria adaptativa
(Sprint 7, Parte B, ver `brand_memory.py`).

Prova, ao vivo, uma unica alegacao: um dominio que o Gemini classificou
ERRADO (falso positivo, rejeitado por um revisor humano) passa a ser
classificado CERTO na proxima chamada -- SEM retreinar nada, SEM trocar de
modelo, so porque a rejeicao humana virou uma entrada em `brand_memory` e
foi injetada como few-shot. As DUAS chamadas ao Gemini abaixo (baseline
"SEM memoria" e "COM memoria") usam o MESMO conteudo raspado, a MESMA
marca, o MESMO dominio -- o UNICO delta entre elas e a presenca do bloco
de memoria no prompt. Isso isola a variavel que importa.

## Dois modos

  python replay_investigation.py
      Modo demonstracao (padrao): cenario sintetico controlado e
      reproduzivel -- nao depende de nenhum site estar no ar nem do
      dominio ja ter sido investigado de verdade. Ainda assim faz DUAS
      chamadas REAIS ao Gemini via Vertex AI (exige GCP configurado) e
      grava a entrada de memoria de verdade em `brand_memory` (Firestore
      real) -- so o "conteudo raspado" e fixo, para a comparacao ser
      honesta e a demo ser gravavel sem depender de rede externa instavel.
      Neste modo, os logs JSON estruturados (ver `telemetry.py`) sao
      suprimidos (`LOG_LEVEL=CRITICAL`) para a saida ficar limpa numa
      gravacao -- so os `print()` deste script aparecem.

  python replay_investigation.py <dominio>
      Modo real: carrega o dossie de `investigations/{dominio}` (precisa
      existir e ter `status == "REJECTED"` -- ou seja, precisa ja ter sido
      rejeitado por um humano no dashboard, ou sincronizado via
      `sync_brand_memory.py`). Re-raspa o dominio AO VIVO (o conteudo
      original nao fica persistido em nenhum lugar do pipeline, so o
      `reasoning` do LLM) -- se o site mudou ou saiu do ar desde a
      investigacao original, a comparacao ainda e honesta (mesmo conteudo
      nas duas chamadas), so pode nao reproduzir o erro original.

Ver a REGRA DA SESSAO sobre verificacao por execucao: os testes deste
arquivo (`tests/test_replay_investigation.py`) mockam `llm_client.generate`
e verificam a LOGICA (o few-shot chega na segunda chamada e so nela); a
alegacao "o Gemini de fato muda de veredito" e uma alegacao sobre o
comportamento do modelo real, que so pode ser verificada rodando este
script contra o Gemini de verdade -- ver o resumo do sprint para o
comando exato.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import textwrap
from datetime import datetime, timezone
from unittest.mock import patch


def _looks_like_demo_invocation(argv: list[str]) -> bool:
    """Heuristica leve para decidir se esta rodando em modo demo (sem
    dominio real) ANTES de importar `plane2_agents.orchestrator` -- esse
    import e o que dispara `telemetry.setup()`, que configura o logging
    JSON estruturado para stdout na hora (nao dentro de nenhuma funcao que
    de para interceptar depois, ver `telemetry.configure_json_logging`).
    Precisa rodar ANTES de qualquer parse "de verdade" via argparse.

    Nunca falha: em duvida (ex: `argv` nao e deste script -- e o caso
    quando este modulo e importado pelos testes sob pytest, cujo `argv` e
    do proprio pytest), assume que NAO e demo e deixa o logging normal --
    nunca suprime log por engano fora de uma execucao real deste script."""
    try:
        if not argv or os.path.basename(argv[0]) not in ("replay_investigation.py", ""):
            return False
        skip_next = False
        for token in argv[1:]:
            if skip_next:
                skip_next = False
                continue
            if token in ("--brand", "--limit"):
                skip_next = True
                continue
            if token.startswith("-"):
                continue
            return False  # ha um dominio posicional -> modo real
        return True  # nenhum positional -> modo demo
    except Exception:
        return False


# Logs JSON estruturados (ver telemetry.configure_json_logging) poluem a
# gravacao do modo demo -- "a saida precisa estar limpa para gravacao de
# video" (pedido explicito). So suprime em modo demo; modo real mantem o
# log normal (auditoria de uma investigacao de verdade continua visivel).
# `setdefault`: nunca sobrescreve um LOG_LEVEL que o operador ja tenha
# configurado explicitamente no ambiente.
if _looks_like_demo_invocation(sys.argv):
    os.environ.setdefault("LOG_LEVEL", "CRITICAL")

import brand_memory
import plane2_agents.orchestrator as orchestrator
from config import settings

# --- saida em tela -----------------------------------------------------

_USE_COLOR = sys.stdout.isatty()
_WIDTH = 78


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def _bold(t: str) -> str:
    return _c("1", t)


def _dim(t: str) -> str:
    return _c("2", t)


def _red(t: str) -> str:
    return _c("31", t)


def _green(t: str) -> str:
    return _c("32", t)


def _yellow(t: str) -> str:
    return _c("33", t)


def _classification_colored(classification: str) -> str:
    return _red(classification) if classification == "MALICIOUS" else _green(classification)


def _pad(text: str, width: int) -> str:
    """Preenche `text` com espacos ANTES de aplicar cor -- `f'{_bold(t):<N}'`
    conta os codigos ANSI (invisiveis) como largura e desalinha a coluna
    assim que uma cor entra em cena. Sempre padroniza a largura no texto
    puro primeiro, colore depois."""
    return text + " " * max(0, width - len(text))


def _rule(char: str = "=") -> None:
    print(char * _WIDTH)


def _title(text: str) -> None:
    _rule()
    print(_bold(f"  {text}"))
    _rule()


def _step(n: int, total: int, text: str) -> None:
    print()
    print(_bold(f"[{n}/{total}] {text}"))
    print(_dim("-" * _WIDTH))


_LABEL_WIDTH = 26


def _field(label: str, value: str) -> None:
    """Imprime `label: value`, com `value` quebrado em multiplas linhas
    (indentadas sob a coluna do valor) quando nao cabe na largura do
    terminal -- reasoning/justificativas do LLM/humano costumam ser
    longos, e uma linha unica cortando no meio fica ilegivel numa
    gravacao."""
    prefix = f"  {_pad(label + ':', _LABEL_WIDTH)} "
    wrapped = textwrap.wrap(value, width=max(_WIDTH - len(prefix), 20)) or [""]
    print(_dim(prefix) + wrapped[0])
    for line in wrapped[1:]:
        print(" " * len(prefix) + line)


# --- cenario de demonstracao (autocontido, sem dependencia de rede) -------

DEMO_BRAND_ID = "nubank"
DEMO_DOMAIN = "nubank-parceiros-cartao.com.br"
DEMO_SCRAPED_CONTENT = (
    "Bem-vindo ao Portal de Parceiros Nubank Cartao Consignado. Este site e "
    "operado pela XPTO Servicos Financeiros Ltda, parceira autorizada para "
    "originacao de cartao consignado com desconto em folha sob a marca "
    "Nubank. Para simular sua proposta, informe seus dados no formulario "
    "abaixo. Duvidas: SAC (11) 4000-0000, atendimento@xpto-parceiros.com.br."
)
DEMO_ORIGINAL_CLASSIFICATION = "MALICIOUS"
DEMO_ORIGINAL_CONFIDENCE = 0.63
DEMO_ORIGINAL_REASONING = (
    "O dominio combina o token 'nubank' com termos financeiros ('cartao', "
    "'parceiros'), um padrao comum de typosquatting contra marcas "
    "bancarias -- classificado como suspeito por similaridade de nome, sem "
    "confirmacao adicional do vinculo comercial."
)
DEMO_REJECTED_BY = "ana.reviewer@empresa.com"
DEMO_REJECTED_AT = datetime(2026, 8, 10, 14, 32, tzinfo=timezone.utc)
DEMO_REJECTION_REASON = (
    "Confirmado com o time de Parcerias Nubank: XPTO e parceira oficial "
    "autorizada para originacao de cartao consignado white-label sob a "
    "marca. Dominio legitimo, nao e phishing -- falso positivo do modelo."
)


def _demo_investigation() -> dict:
    return {
        "domain": DEMO_DOMAIN,
        "matched_brand": DEMO_BRAND_ID,
        "classification": DEMO_ORIGINAL_CLASSIFICATION,
        "confidence": DEMO_ORIGINAL_CONFIDENCE,
        "reasoning": DEMO_ORIGINAL_REASONING,
        "status": "REJECTED",
        "rejected_by": DEMO_REJECTED_BY,
        "rejected_at": DEMO_REJECTED_AT.isoformat(),
        "rejection_reason": DEMO_REJECTION_REASON,
    }


# --- carregamento (modo real) --------------------------------------------


def _load_real_investigation(domain: str) -> dict:
    investigation = orchestrator._get_cached_investigation(domain)
    if investigation is None:
        print(_red(f"ERRO: nenhum dossie encontrado para '{domain}' em investigations/."))
        print("Investigue o dominio primeiro (pipeline normal), ou rode sem argumento para o modo demo.")
        sys.exit(1)
    if investigation.get("status") != "REJECTED":
        print(
            _red(
                f"ERRO: dossie de '{domain}' existe mas status e "
                f"{investigation.get('status')!r}, nao 'REJECTED'."
            )
        )
        print(
            "Este script demonstra a correcao de um FALSO POSITIVO -- o dominio precisa ter sido "
            "rejeitado por um humano no dashboard (ou via sync_brand_memory.py) antes do replay."
        )
        sys.exit(1)
    if not investigation.get("matched_brand"):
        print(_red(f"ERRO: dossie de '{domain}' nao tem 'matched_brand' -- nao ha marca para rotear."))
        sys.exit(1)
    return investigation


# --- fluxo principal -------------------------------------------------------


async def _classify_with_fixed_content(
    domain: str, brand_id: str, content: str, few_shot_examples: list | None
):
    """Roda `classify_domain_with_gemini` de verdade (chamada REAL ao
    Gemini), so substituindo `scrape_website` por um valor fixo -- garante
    que as duas chamadas comparadas (com/sem memoria) recebem EXATAMENTE o
    mesmo conteudo, isolando a memoria como unica variavel."""
    with patch.object(orchestrator, "scrape_website", lambda url: content):
        return await orchestrator.classify_domain_with_gemini(domain, brand_id, few_shot_examples)


async def run_replay(domain: str | None, *, brand_override: str | None, limit: int) -> int:
    total_steps = 5
    demo_mode = domain is None

    _title("SENTINEL -- REPLAY DE INVESTIGACAO (memoria adaptativa, sem retreino)")
    if demo_mode:
        print(_dim("  Modo: DEMONSTRACAO (cenario sintetico e reproduzivel, chamadas reais ao Gemini)"))
    else:
        print(_dim(f"  Modo: REAL -- dossie existente de '{domain}'"))

    # --- [1/5] carregar o dossie original -----------------------------
    _step(1, total_steps, "Carregando dossie original (falso positivo confirmado por humano)")
    if demo_mode:
        investigation = _demo_investigation()
        target_domain = DEMO_DOMAIN
    else:
        investigation = _load_real_investigation(domain)
        target_domain = domain
    brand_id = brand_override or investigation["matched_brand"]

    _field("dominio", _bold(target_domain))
    _field("marca", brand_id)
    _field(
        "classificacao original",
        f"{_classification_colored(investigation['classification'])} "
        f"(confianca {investigation['confidence']:.2f})",
    )
    _field("reasoning original", investigation["reasoning"])
    _field("rejeitado por", investigation.get("rejected_by", "?"))
    _field("motivo da rejeicao", investigation.get("rejection_reason", "?"))
    print()
    print(f"  {_red('✗')} Isso foi um FALSO POSITIVO -- o modelo errou, um humano corrigiu.")

    # --- [2/5] gravar a memoria -----------------------------------------
    _step(2, total_steps, "Gravando a rejeicao em brand_memory (Firestore real, persistente)")
    entry = brand_memory.record_rejection(brand_id=brand_id, domain=target_domain, investigation=investigation)
    print(f"  {_green('✓')} brand_memory/{entry.brand_id}__{entry.domain}__{entry.decision_type}__...")
    _field("versao da memoria", str(entry.memory_version))
    _field("gravada em", entry.created_at.isoformat())

    # --- conteudo usado nas duas chamadas (identico) ---------------------
    if demo_mode:
        content = DEMO_SCRAPED_CONTENT
    else:
        print()
        print(_dim("  Re-raspando o dominio ao vivo para a comparacao (conteudo original nao fica"))
        print(_dim("  persistido em nenhum lugar do pipeline, so o reasoning) ..."))
        content = await asyncio.to_thread(orchestrator.scrape_website, f"https://{target_domain}")

    # --- [3/5] baseline sem memoria --------------------------------------
    _step(3, total_steps, "Reclassificando SEM memoria (baseline -- mesmo prompt de sempre)")
    baseline_result, baseline_usage, _, baseline_cost, baseline_mem = await _classify_with_fixed_content(
        target_domain, brand_id, content, None
    )
    print(
        f"  -> {_classification_colored(baseline_result.classification)} "
        f"(confianca {baseline_result.confidence:.2f})"
    )
    _field("reasoning", baseline_result.reasoning)

    # --- [4/5] com memoria ------------------------------------------------
    _step(4, total_steps, f"Reclassificando COM memoria (+ ate {limit} exemplo(s) few-shot desta marca)")
    few_shot_examples = brand_memory.get_relevant_memories(brand_id, target_domain, limit=limit)
    print(f"  exemplos recuperados de brand_memory: {len(few_shot_examples)}")
    for i, ex in enumerate(few_shot_examples, 1):
        print(f"    {i}. {ex.domain} -> {ex.decision_type} ({ex.human_rationale[:60]}...)")

    memory_result, memory_usage_llm, _, memory_cost, memory_mem = await _classify_with_fixed_content(
        target_domain, brand_id, content, few_shot_examples
    )
    print(
        f"  -> {_classification_colored(memory_result.classification)} "
        f"(confianca {memory_result.confidence:.2f})"
    )
    _field("reasoning", memory_result.reasoning)

    # --- [5/5] resumo -------------------------------------------------------
    _step(5, total_steps, "Resumo")
    _print_summary_table(
        baseline_result, baseline_usage, baseline_cost, baseline_mem,
        memory_result, memory_usage_llm, memory_cost, memory_mem,
    )

    corrected = (
        baseline_result.classification == "MALICIOUS"
        and memory_result.classification == "SAFE"
    )
    print()
    _rule()
    if corrected:
        print(_bold(_green("  ✅ CORRIGIDO VIA MEMORIA -- SEM RETREINO, SEM MUDAR O MODELO.")))
        print(
            "     O unico delta entre as duas chamadas ao Gemini acima foi "
            f"{memory_mem.examples_injected} exemplo(s)"
        )
        print("     de brand_memory injetado(s) no prompt. Nenhum peso do modelo mudou.")
    elif baseline_result.classification == memory_result.classification:
        print(_bold(_yellow("  ⚠ SEM MUDANCA -- o modelo manteve o mesmo veredito nas duas chamadas.")))
        print("     Isso pode acontecer (o Gemini nao e deterministico a 100% mesmo com temperature")
        print("     baixa) -- rode de novo, ou aumente --limit para injetar mais exemplos.")
    else:
        print(_bold(_yellow("  ⚠ MUDOU, mas nao para o veredito esperado -- reveja o cenario/exemplos.")))
    _rule()

    return 0 if corrected else 1


def _print_summary_table(
    baseline_result, baseline_usage, baseline_cost, baseline_mem,
    memory_result, memory_usage_llm, memory_cost, memory_mem,
) -> None:
    rows = [
        ("classificacao", baseline_result.classification, memory_result.classification),
        ("confianca", f"{baseline_result.confidence:.2f}", f"{memory_result.confidence:.2f}"),
        ("tokens de entrada (real)", str(baseline_usage.input_tokens), str(memory_usage_llm.input_tokens)),
        ("exemplos few-shot injetados", str(baseline_mem.examples_injected), str(memory_mem.examples_injected)),
        (
            "tokens extra estimados (few-shot)",
            str(baseline_mem.estimated_extra_input_tokens),
            str(memory_mem.estimated_extra_input_tokens),
        ),
        ("custo total estimado (USD)", f"${baseline_cost:.6f}", f"${memory_cost:.6f}"),
        (
            "  dos quais, custo do few-shot",
            f"${baseline_mem.estimated_extra_cost_usd:.6f}",
            f"${memory_mem.estimated_extra_cost_usd:.6f}",
        ),
    ]
    label_w = 34
    col_w = 16
    print(f"  {_pad('', label_w)} {_bold(_pad('SEM memoria', col_w))} {_bold(_pad('COM memoria', col_w))}")
    print(f"  {'-' * label_w} {'-' * col_w} {'-' * col_w}")
    for label, a, b in rows:
        # Sempre padroniza a largura no texto PURO primeiro (`_pad`), so
        # entao envolve em cor -- ver docstring de `_pad` sobre por que a
        # ordem inversa desalinha a coluna assim que uma cor entra em cena.
        a_cell = _pad(a, col_w)
        b_cell = _pad(b, col_w)
        if label == "classificacao":
            a_cell = _classification_colored(a) + a_cell[len(a):]
            b_cell = _classification_colored(b) + b_cell[len(b):]
        print(f"  {_pad(label, label_w)} {a_cell} {b_cell}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "domain",
        nargs="?",
        default=None,
        help="Dominio real ja rejeitado (status REJECTED em investigations/). "
        "Sem argumento, roda o cenario de demonstracao sintetico.",
    )
    parser.add_argument("--brand", default=None, help="Sobrescreve a marca (default: matched_brand do dossie)")
    parser.add_argument(
        "--limit",
        type=int,
        default=max(settings.brand_memory_max_examples, 1),
        help="Numero maximo de exemplos few-shot a injetar (default: settings.brand_memory_max_examples, minimo 1)",
    )
    args = parser.parse_args()

    exit_code = asyncio.run(run_replay(args.domain, brand_override=args.brand, limit=args.limit))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
