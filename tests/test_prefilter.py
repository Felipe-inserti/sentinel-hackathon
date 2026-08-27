"""Testes de `plane1_ingestion/prefilter.py`.

Cobre especificamente o bug corrigido nesta sprint (ver FINDINGS.md SS10):
`analyze_domain` so gravava a distancia de edicao QUALIFICADA
(`best_distance`) quando ela tambem batia o melhor score de similaridade
de token ja visto -- um match de distancia de edicao genuino era
descartado silenciosamente sempre que QUALQUER OUTRA coisa (mesmo ainda
abaixo do limiar de suspeita) tivesse pontuado mais alto antes. Medido
contra um corpus sintetico de 5.413 typosquats classicos: recall de
homoglyph/transposicao de "loggi" era 0% por causa disso, independente do
VALOR de `max_edit_distance` escolhido -- o bug, nao o threshold, era a
causa da cegueira.

Nao existia `tests/test_prefilter.py` antes desta sprint -- o modulo so
tinha cobertura indireta via `tests/test_ct_listener_*`."""

from __future__ import annotations

import Levenshtein

from plane1_ingestion.prefilter import DEFAULT_MAX_EDIT_DISTANCE, analyze_domain


def test_default_max_edit_distance_is_1():
    """Pin de regressao do valor escolhido nesta sprint (medido: recall
    identico a distance=2 nos corpus real e sintetico, ~92% menos ruido --
    ver FINDINGS.md SS10)."""
    assert DEFAULT_MAX_EDIT_DISTANCE == 1


# --- Regressao do bug: distancia qualificada nao pode ser descartada -------


def test_qualified_edit_distance_match_is_not_discarded_by_lower_token_score():
    """Caso minimo que reproduz o bug: 'olggi.com' tem Levenshtein.ratio
    contra 'loggi' = 0.8 (abaixo do limiar 0.82, NAO suspeito por si so),
    mas a distancia de edicao contra 'loggi' e 1 (dentro do
    max_edit_distance default). Antes do fix, o score de token (0.8, ja
    computado e MAIOR que o equivalent_ratio de 0.8 -- empate que o `>`
    estrito do codigo original tratava como "nao supera") fazia
    `best_distance` nunca ser gravado -- `is_suspicious` ficava False."""
    assert Levenshtein.ratio("olggi", "loggi") < 0.82  # nao suspeito via similaridade pura

    result = analyze_domain("olggi.com")

    assert result.is_suspicious is True
    assert result.matched_brand == "loggi"
    assert result.edit_distance == 1


def test_loggi_adjacent_transposition_and_homoglyph_within_distance_1():
    """Amostra do corpus sintetico desta sprint -- transposicoes/homoglyphs
    de 'loggi' com distancia de edicao real = 1, que tinham recall 0%
    antes do fix."""
    for domain in ("logig.com", "olggi.com", "loggl.com"):
        result = analyze_domain(domain)
        assert result.is_suspicious is True, f"{domain} deveria ser suspeito (regressao do bug)"
        assert result.matched_brand == "loggi"


def test_loggi_transposition_requiring_distance_2_is_not_caught_at_default():
    """Documenta o trade-off REAL (nao o que se assumia antes) de
    max_edit_distance=1 vs 2: 'lgogi.com' tem distancia de edicao PADRAO
    (Levenshtein, nao Damerau -- uma transposicao adjacente custa 2, nao 1)
    igual a 2 contra 'loggi' -- fica de fora do default atual (1), mas
    seria pego com max_edit_distance=2 explicito. Medido no corpus
    sintetico: essa e a UNICA categoria com diferenca real de recall entre
    os dois thresholds (99.6% vs 99.9%) -- ver FINDINGS.md SS10."""
    assert Levenshtein.distance("lgogi", "loggi") == 2

    default_result = analyze_domain("lgogi.com")
    assert default_result.is_suspicious is False

    explicit_distance_2 = analyze_domain("lgogi.com", max_edit_distance=2)
    assert explicit_distance_2.is_suspicious is True
    assert explicit_distance_2.matched_brand == "loggi"


# --- Exemplo trazido na discussao desta sprint ------------------------------


def test_nubarnk_is_caught_via_token_similarity_regardless_of_edit_distance():
    """'nubarnk.com' (insercao de 1 char em 'nubank') tem
    Levenshtein.distance = 1, nao 2 -- e pego pelo caminho de similaridade
    de TOKEN (score >= 0.82), que nao depende de `max_edit_distance` --
    verificado nos tres valores para confirmar que a escolha do threshold
    e irrelevante para este caso especifico."""
    assert Levenshtein.distance("nubank", "nubarnk") == 1

    for max_edit_distance in (0, 1, 2):
        result = analyze_domain("nubarnk.com", max_edit_distance=max_edit_distance)
        assert result.is_suspicious is True
        assert result.matched_brand == "nubank"
        assert result.score >= 0.82


# --- Casos basicos (allowlist, marca exata, dominio irrelevante) -----------


def test_trusted_domain_is_never_suspicious():
    result = analyze_domain("nubank.com.br")
    assert result.is_suspicious is False
    assert "allowlist" in result.reason


def test_exact_brand_token_outside_allowlist_is_suspicious():
    result = analyze_domain("nubank-seguranca.xyz")
    assert result.is_suspicious is True
    assert result.matched_brand == "nubank"


def test_unrelated_domain_is_not_suspicious():
    result = analyze_domain("google.com")
    assert result.is_suspicious is False
    assert result.matched_brand is None
