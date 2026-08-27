"""Plano 1 - Pre-filtro de dominios (Zero LLM).

Matematica pura (Levenshtein + normalizacao de homoglyphs) para decidir, em
microssegundos e sem custo de tokens, se um dominio recem-emitido em um
certificado TLS merece ser escalado para investigacao pela IA (Plano 2).

Este modulo NAO faz nenhuma chamada de rede ou de IA. E o "escudo de custos"
do Sentinel: o objetivo e descartar ~99% do ruido do Certificate Transparency
antes que qualquer token seja gasto.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import Levenshtein

logger = logging.getLogger(__name__)

# Marcas monitoradas (alvos frequentes de phishing B2B/fintech no Brasil).
MONITORED_BRANDS: tuple[str, ...] = (
    "nubank",
    "loggi",
    "ifood",
)

# Dominios legitimos conhecidos das marcas monitoradas: nunca devem ser
# escalados, mesmo tendo similaridade maxima com a propria marca.
TRUSTED_DOMAINS: frozenset[str] = frozenset(
    {
        "nubank.com.br",
        "nu.com.br",
        "loggi.com",
        "ifood.com.br",
    }
)

# Homoglyphs comuns (cirilico/latin look-alikes e leetspeak) usados em
# typosquatting para enganar tanto humanos quanto matchers ingenuos.
_HOMOGLYPH_MAP: dict[str, str] = {
    "а": "a",  # cyrillic a
    "е": "e",  # cyrillic e
    "о": "o",  # cyrillic o
    "р": "p",  # cyrillic p
    "с": "c",  # cyrillic c
    "х": "x",  # cyrillic x
    "у": "y",  # cyrillic u
    "і": "i",  # cyrillic i
    "ј": "j",  # cyrillic j
    "ѕ": "s",  # cyrillic s
    "ⅼ": "l",  # small roman numeral l
    "0": "o",
    "1": "l",
    "3": "e",
    "4": "a",
    "5": "s",
    "7": "t",
    "8": "b",
    "$": "s",
    "@": "a",
}
_HOMOGLYPH_TABLE = str.maketrans(_HOMOGLYPH_MAP)

# TLDs comuns que devem ser descartados ao isolar o "label" registravel do
# dominio. Nao precisa ser uma lista exaustiva (PSL completa) para o prefiltro.
_COMMON_TLD_RE = re.compile(
    r"\.(com|net|org|info|biz|xyz|top|online|site|shop|app|dev|co)"
    r"(\.(br|us|uk|io))?$"
)

# Similaridade minima (0-1) entre um token do dominio e uma marca para ser
# considerado suspeito.
DEFAULT_SIMILARITY_THRESHOLD = 0.82

# Distancia de edicao maxima aceita numa janela deslizante contra a marca,
# usada para pegar casos tipo "nubank-suporte-oficial.com".
#
# Baixado de 2 para 1 (sprint de medicao de custo real, ver FINDINGS.md
# SS10): medido contra 28.515 escapes REAIS de um run de 31min contra o
# Certificate Transparency + um corpus sintetico de 5.413 typosquats
# classicos (homoglyph/insercao/delecao/transposicao/duplo-edit) -- recall
# e IDENTICO em distance=2 e distance=1 (95.5% antes do fix abaixo, 99.9%
# vs 99.6% depois dele) porque todo typosquat classico ja e pego pelo
# caminho de similaridade de token (Levenshtein.ratio >= threshold),
# independente deste parametro. distance=2 so adicionava ruido: 92% dos
# escapes reais eram hashes/IDs de infraestrutura legitima de alta entropia
# (WorkDay, Synology myvolumio, Cloudflare Workers/Pages) colidindo por
# acaso dentro de 2 edicoes -- nunca ataques de verdade.
DEFAULT_MAX_EDIT_DISTANCE = 1


@dataclass(frozen=True)
class DomainRiskAssessment:
    """Resultado detalhado do prefiltro para um dominio."""

    domain: str
    is_suspicious: bool
    matched_brand: str | None = None
    score: float = 0.0
    edit_distance: int | None = None
    reason: str = ""
    tokens: tuple[str, ...] = field(default_factory=tuple)
    # Quais familias de heuristica contribuiram para o score -- consumido
    # pela camada de triagem Gemma (gemma_triage.py) como sinal estruturado,
    # nao muda a decisao is_suspicious/score em si.
    heuristics_triggered: tuple[str, ...] = field(default_factory=tuple)


# Subconjunto de _HOMOGLYPH_MAP que sao letras cirilicas visualmente
# parecidas com letras latinas (nao digitos/simbolos de leetspeak) --
# usado so para rotular qual familia de heuristica disparou.
_CYRILLIC_LOOKALIKE_CHARS = frozenset("аеорсхуіјѕⅼ")
_LEETSPEAK_CHARS = frozenset("013457$@")


def _detect_heuristics(raw_domain: str, edit_distance: int | None) -> tuple[str, ...]:
    lowered = raw_domain.strip().lower()
    detected: list[str] = []
    if any(ch in _CYRILLIC_LOOKALIKE_CHARS for ch in lowered):
        detected.append("homoglyph")
    if any(ch in _LEETSPEAK_CHARS for ch in lowered):
        detected.append("leetspeak")
    if edit_distance is not None:
        detected.append("sliding_window")
    return tuple(detected)


def _strip_scheme_and_path(raw_domain: str) -> str:
    domain = raw_domain.strip().lower()
    domain = re.sub(r"^[a-z]+://", "", domain)
    domain = domain.split("/", 1)[0]
    domain = domain.split(":", 1)[0]  # remove porta, se houver
    if domain.startswith("*."):
        domain = domain[2:]
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def _apply_homoglyph_normalization(text: str) -> str:
    return text.translate(_HOMOGLYPH_TABLE)


def normalize_domain(raw_domain: str) -> str:
    """Normaliza um dominio bruto: minusculas, sem scheme/porta/path/www e
    com homoglyphs traduzidos para o equivalente ASCII."""
    domain = _strip_scheme_and_path(raw_domain)
    return _apply_homoglyph_normalization(domain)


def _extract_tokens(normalized_domain: str) -> tuple[str, ...]:
    """Extrai o(s) label(s) registraveis do dominio, separados por ponto ou
    hifen, descartando o TLD. Ex: 'nub4nk-suporte.com.br' -> ('nub4nk',
    'suporte')."""
    without_tld = _COMMON_TLD_RE.sub("", normalized_domain)
    raw_tokens = re.split(r"[.\-_]", without_tld)
    return tuple(t for t in raw_tokens if t)


def _sliding_window_min_distance(haystack: str, needle: str) -> int:
    """Menor distancia de Levenshtein entre `needle` e qualquer substring de
    `haystack` com tamanho proximo ao de `needle` (+/- 2 caracteres).

    Captura padroes de typosquat embutidos em dominios mais longos, como
    'nubank-suporte-oficial.com' contra a marca 'nubank'.
    """
    if not haystack or not needle:
        return len(needle)

    best = len(needle)
    for delta in (-2, -1, 0, 1, 2):
        window_size = len(needle) + delta
        if window_size <= 0 or window_size > len(haystack):
            continue
        for start in range(0, len(haystack) - window_size + 1):
            window = haystack[start : start + window_size]
            distance = Levenshtein.distance(window, needle)
            if distance < best:
                best = distance
                if best == 0:
                    return 0
    return best


def analyze_domain(
    raw_domain: str,
    brands: tuple[str, ...] = MONITORED_BRANDS,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    max_edit_distance: int = DEFAULT_MAX_EDIT_DISTANCE,
) -> DomainRiskAssessment:
    """Avalia o risco de um dominio contra a lista de marcas monitoradas.

    Combina duas heuristicas puramente matematicas:
      1. Razao de similaridade (Levenshtein.ratio) entre cada token do
         dominio e a marca.
      2. Menor distancia de edicao encontrada numa janela deslizante sobre o
         dominio inteiro (concatenado, sem separadores), para pegar marcas
         "escondidas" em dominios mais longos.
    """
    normalized = normalize_domain(raw_domain)

    if normalized in TRUSTED_DOMAINS:
        return DomainRiskAssessment(
            domain=raw_domain,
            is_suspicious=False,
            reason="dominio na allowlist de marcas legitimas",
        )

    tokens = _extract_tokens(normalized)
    concatenated = re.sub(r"[^a-z0-9]", "", normalized)

    best_score = 0.0
    best_brand: str | None = None
    # Distancia de edicao QUALIFICADA (<= max_edit_distance) mais proxima
    # ja vista -- gravada de forma INDEPENDENTE de best_score (ver bug
    # corrigido abaixo, FINDINGS.md SS10). O sinal do sliding_window e uma
    # alternativa "OR" ao sinal de similaridade de token, nao um
    # competidor: mesmo quando o token de outra marca/palavra pontua mais
    # alto (e ainda assim fica abaixo do limiar de suspeita), um match de
    # distancia de edicao genuino nao pode ser descartado por isso.
    best_distance: int | None = None

    for brand in brands:
        for token in tokens:
            ratio = Levenshtein.ratio(token, brand)
            if ratio > best_score:
                best_score = ratio
                best_brand = brand

        distance = _sliding_window_min_distance(concatenated, brand)
        if distance <= max_edit_distance:
            # BUG CORRIGIDO (FINDINGS.md SS10): antes, esta atribuicao so
            # acontecia dentro do "if equivalent_ratio > best_score"
            # abaixo -- um match de distancia de edicao QUALIFICADO era
            # descartado sempre que o score de similaridade de token (por
            # mais que ainda estivesse abaixo do limiar de suspeita) fosse
            # maior. Recall medido em typosquats sinteticos de "loggi"
            # (transposicao adjacente, homoglyph) era 0% por causa disso --
            # independente do VALOR de max_edit_distance escolhido.
            if best_distance is None or distance < best_distance:
                best_distance = distance
                if best_brand is None:
                    best_brand = brand
            equivalent_ratio = 1 - (distance / max(len(brand), 1))
            if equivalent_ratio > best_score:
                best_score = equivalent_ratio
                best_brand = brand

    is_exact_brand_match = best_brand is not None and best_brand in tokens
    is_suspicious = bool(best_brand) and (
        best_score >= similarity_threshold and not is_exact_brand_match
        or (best_distance is not None and best_distance <= max_edit_distance)
    )

    # Dominio identico a marca (ex: token == "nubank") mas fora da allowlist
    # de dominios confiaveis e, por definicao, ainda mais suspeito.
    if is_exact_brand_match:
        is_suspicious = True

    reason = (
        f"similaridade {best_score:.2f} com marca '{best_brand}'"
        if is_suspicious
        else "sem similaridade relevante com marcas monitoradas"
    )

    return DomainRiskAssessment(
        domain=raw_domain,
        is_suspicious=is_suspicious,
        matched_brand=best_brand if is_suspicious else None,
        score=round(best_score, 4),
        edit_distance=best_distance,
        reason=reason,
        tokens=tokens,
        heuristics_triggered=_detect_heuristics(raw_domain, best_distance) if is_suspicious else (),
    )


def is_suspicious(raw_domain: str, brands: tuple[str, ...] = MONITORED_BRANDS) -> bool:
    """Interface simples: True se o dominio merece ser escalado ao Plano 2."""
    try:
        return analyze_domain(raw_domain, brands=brands).is_suspicious
    except Exception:
        logger.exception("Falha ao analisar dominio %r no prefiltro", raw_domain)
        # Em duvida (erro de parsing), falha para o lado seguro: nao descarta.
        return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    samples = [
        "nubank.com.br",
        "nu-bank-seguro.com",
        "nub4nk-suporte-oficial.xyz",
        "loggi-entregas-rastreio.com",
        "1food-cupons.com",
        "google.com",
        "meusite-pessoal.dev",
    ]
    for sample in samples:
        result = analyze_domain(sample)
        print(f"{sample!r:45} -> suspicious={result.is_suspicious!s:5} "
              f"brand={result.matched_brand} score={result.score}")
