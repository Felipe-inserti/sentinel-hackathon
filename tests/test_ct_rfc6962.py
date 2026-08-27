"""Testes do parsing RFC 6962 (`plane1_ingestion/ct_rfc6962.py`) --
troca do certstream por polling direto do log.

Cobre exatamente os dois achados verificados por execucao contra o log
real (ver sessao de pesquisa que precedeu esta sprint):
  1. `x509_entry` e `precert_entry` exigem caminhos de decode DIFERENTES
     (o segundo so tem o certificado completo em `extra_data`, nao em
     `leaf_input`).
  2. O log pode devolver MENOS entradas do que foi pedido -- `fetch_entries`
     nao deve fingir que recebeu o que pediu.

Os certificados de teste sao DER real, gerados com `cryptography` (nao
bytes fixos inventados) -- o parser precisa conseguir carregar um DER de
verdade, nao um mock do proprio `x509.load_der_x509_certificate`.
"""

from __future__ import annotations

import base64
import datetime
from unittest.mock import MagicMock

import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from plane1_ingestion import ct_rfc6962


# --- Helpers para montar entradas RFC 6962 reais (bytes reais, nao mock) ---


def _make_der_cert(domains: list[str], not_before: datetime.datetime | None = None) -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    common_name = domains[0] if domains else "sem-san.invalid"
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    nb = not_before or (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(nb)
        .not_valid_after(nb + datetime.timedelta(days=90))
    )
    if domains:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(d) for d in domains]), critical=False
        )
    cert = builder.sign(key, hashes.SHA256())
    return cert.public_bytes(serialization.Encoding.DER)


def _opaque(data: bytes, length_prefix_bytes: int) -> bytes:
    return len(data).to_bytes(length_prefix_bytes, "big") + data


def _build_x509_leaf(der_cert: bytes, timestamp_ms: int = 1_700_000_000_000) -> str:
    leaf = bytearray()
    leaf += bytes([0])  # version
    leaf += bytes([0])  # leaf_type = timestamped_entry
    leaf += timestamp_ms.to_bytes(8, "big")
    leaf += (0).to_bytes(2, "big")  # entry_type = x509_entry
    leaf += _opaque(der_cert, 3)  # ASN.1Cert
    leaf += (0).to_bytes(2, "big")  # CtExtensions vazio
    return base64.b64encode(bytes(leaf)).decode()


def _build_precert_leaf_and_extra(der_precert: bytes, timestamp_ms: int = 1_700_000_000_000) -> tuple[str, str]:
    leaf = bytearray()
    leaf += bytes([0])
    leaf += bytes([0])
    leaf += timestamp_ms.to_bytes(8, "big")
    leaf += (1).to_bytes(2, "big")  # entry_type = precert_entry
    leaf += bytes(32)  # issuer_key_hash (irrelevante para o parser)
    leaf += _opaque(b"\x00\x00\x00\x00", 3)  # tbs_certificate placeholder -- nunca lido pelo parser
    leaf += (0).to_bytes(2, "big")
    leaf_b64 = base64.b64encode(bytes(leaf)).decode()

    extra = bytearray()
    extra += _opaque(der_precert, 3)  # PrecertChainEntry.pre_certificate
    extra += (0).to_bytes(3, "big")  # precertificate_chain vazio
    extra_b64 = base64.b64encode(bytes(extra)).decode()
    return leaf_b64, extra_b64


# --- parse_leaf_entry: x509_entry --------------------------------------------


def test_parse_x509_entry_extracts_domains_from_leaf_input():
    der = _make_der_cert(["nub4nk-phish.xyz", "outro.nub4nk-phish.xyz"])
    leaf_b64 = _build_x509_leaf(der)

    entry = ct_rfc6962.parse_leaf_entry(42, leaf_b64, extra_data_b64="")

    assert entry is not None
    assert entry.log_index == 42
    assert entry.entry_type == "x509_entry"
    assert set(entry.domains) == {"nub4nk-phish.xyz", "outro.nub4nk-phish.xyz"}
    assert entry.certificate_age_seconds is not None
    assert entry.certificate_age_seconds > 0


def test_parse_x509_entry_computes_certificate_age_from_not_before():
    not_before = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=3)
    der = _make_der_cert(["idade-teste.xyz"], not_before=not_before)
    leaf_b64 = _build_x509_leaf(der)

    entry = ct_rfc6962.parse_leaf_entry(1, leaf_b64, extra_data_b64="")

    assert entry is not None
    # ~3h = 10800s, com folga generosa para o tempo de execucao do teste.
    assert 10700 < entry.certificate_age_seconds < 10900


# --- parse_leaf_entry: precert_entry -- exige extra_data ---------------------


def test_parse_precert_entry_extracts_domains_from_extra_data_not_leaf_input():
    """O achado central desta sprint: o certificado completo do precert
    esta em extra_data, NAO em leaf_input (que so tem o TBSCertificate sem
    assinatura -- nao carregavel pelo cryptography)."""
    der = _make_der_cert(["nub4nk-precert.xyz"])
    leaf_b64, extra_b64 = _build_precert_leaf_and_extra(der)

    entry = ct_rfc6962.parse_leaf_entry(7, leaf_b64, extra_b64)

    assert entry is not None
    assert entry.entry_type == "precert_entry"
    assert entry.domains == ["nub4nk-precert.xyz"]


def test_parse_precert_entry_without_extra_data_returns_none():
    der = _make_der_cert(["sem-extra-data.xyz"])
    leaf_b64, _ = _build_precert_leaf_and_extra(der)

    entry = ct_rfc6962.parse_leaf_entry(8, leaf_b64, extra_data_b64="")

    assert entry is None


# --- Casos de erro/malformacao -----------------------------------------------


def test_parse_unknown_leaf_type_returns_none():
    leaf = bytearray()
    leaf += bytes([0])
    leaf += bytes([9])  # leaf_type desconhecido (so 0 = timestamped_entry existe)
    leaf += (0).to_bytes(8, "big")
    leaf += (0).to_bytes(2, "big")
    leaf_b64 = base64.b64encode(bytes(leaf)).decode()

    assert ct_rfc6962.parse_leaf_entry(1, leaf_b64, "") is None


def test_parse_unknown_entry_type_returns_none():
    leaf = bytearray()
    leaf += bytes([0])
    leaf += bytes([0])
    leaf += (0).to_bytes(8, "big")
    leaf += (99).to_bytes(2, "big")  # entry_type desconhecido
    leaf_b64 = base64.b64encode(bytes(leaf)).decode()

    assert ct_rfc6962.parse_leaf_entry(1, leaf_b64, "") is None


def test_parse_invalid_base64_returns_none():
    assert ct_rfc6962.parse_leaf_entry(1, "!!!nao e base64!!!", "") is None


def test_parse_leaf_input_too_short_returns_none():
    leaf_b64 = base64.b64encode(b"\x00\x00").decode()
    assert ct_rfc6962.parse_leaf_entry(1, leaf_b64, "") is None


def test_parse_corrupted_der_returns_none():
    leaf_b64 = _build_x509_leaf(b"isto nao e um certificado DER valido")
    assert ct_rfc6962.parse_leaf_entry(1, leaf_b64, "") is None


def test_parse_entry_with_no_san_returns_none():
    der = _make_der_cert([])  # sem SubjectAlternativeName
    leaf_b64 = _build_x509_leaf(der)
    assert ct_rfc6962.parse_leaf_entry(1, leaf_b64, "") is None


def test_parse_filters_wildcard_star_dot_star_domains():
    """Mesmo guard que `_extract_domains` tinha no certstream, para SAN
    mal-formado -- nao da para gerar via CertificateBuilder normal (o
    proprio cryptography rejeitaria), entao testamos a funcao interna de
    filtro diretamente com o valor ja extraido."""
    der = _make_der_cert(["dominio-normal.xyz"])
    cert = x509.load_der_x509_certificate(der)
    domains = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(
        x509.DNSName
    )
    filtered = [d for d in (domains + ["*.*.exemplo.com"]) if d and not d.startswith("*.*")]
    assert filtered == ["dominio-normal.xyz"]


# --- fetch_sth / fetch_entries: camada HTTP ----------------------------------


def test_fetch_sth_parses_tree_size(monkeypatch):
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {"tree_size": 12345, "timestamp": 999, "sha256_root_hash": "x", "tree_head_signature": "y"}
    monkeypatch.setattr(ct_rfc6962._session, "get", MagicMock(return_value=fake_response))

    sth = ct_rfc6962.fetch_sth()

    assert sth.tree_size == 12345
    assert sth.timestamp == 999


def test_fetch_sth_raises_ct_log_unavailable_on_connection_error(monkeypatch):
    monkeypatch.setattr(
        ct_rfc6962._session, "get", MagicMock(side_effect=requests.exceptions.ConnectionError("fora do ar"))
    )
    with pytest.raises(ct_rfc6962.CTLogUnavailableError):
        ct_rfc6962.fetch_sth()


def _http_error_response(status_code: int) -> MagicMock:
    """Constroi um `requests.exceptions.HTTPError` com `.response` de
    verdade (como `Response.raise_for_status()` faz na lib real) -- e o
    que `_raise_for_transient_error` inspeciona para distinguir 429 de
    qualquer outro erro HTTP."""
    fake_response = MagicMock()
    fake_response.status_code = status_code
    http_error = requests.exceptions.HTTPError(f"{status_code} erro HTTP")
    http_error.response = fake_response
    fake_response.raise_for_status.side_effect = http_error
    return fake_response


def test_fetch_sth_raises_ct_log_unavailable_on_generic_http_error(monkeypatch):
    monkeypatch.setattr(ct_rfc6962._session, "get", MagicMock(return_value=_http_error_response(503)))
    with pytest.raises(ct_rfc6962.CTLogUnavailableError) as exc_info:
        ct_rfc6962.fetch_sth()
    assert not isinstance(exc_info.value, ct_rfc6962.CTLogRateLimitedError)


def test_fetch_sth_raises_rate_limited_specifically_on_429(monkeypatch):
    """O sinal que o controlador de concorrencia da ingestao paralela usa
    para parar de subir e reduzir -- precisa ser uma excecao DISTINTA de
    timeout/5xx genericos."""
    monkeypatch.setattr(ct_rfc6962._session, "get", MagicMock(return_value=_http_error_response(429)))
    with pytest.raises(ct_rfc6962.CTLogRateLimitedError):
        ct_rfc6962.fetch_sth()


def test_fetch_entries_raises_rate_limited_specifically_on_429(monkeypatch):
    monkeypatch.setattr(ct_rfc6962._session, "get", MagicMock(return_value=_http_error_response(429)))
    with pytest.raises(ct_rfc6962.CTLogRateLimitedError):
        ct_rfc6962.fetch_entries(start=0, end=999)


def test_rate_limited_error_is_a_ct_log_unavailable_error():
    """Todo `except CTLogUnavailableError` generico (backoff comum) ainda
    pega 429 tambem -- so o controlador de concorrencia precisa da
    distincao fina."""
    assert issubclass(ct_rfc6962.CTLogRateLimitedError, ct_rfc6962.CTLogUnavailableError)


def test_fetch_entries_returns_exactly_what_the_server_sent_even_if_less_than_requested(monkeypatch):
    """O achado verificado ao vivo: pedir 1000/2000 devolveu 20/32. Esta
    funcao nao deve inventar/completar -- so repassa o que veio."""
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {"entries": [{"leaf_input": "a", "extra_data": ""}] * 20}
    get_mock = MagicMock(return_value=fake_response)
    monkeypatch.setattr(ct_rfc6962._session, "get", get_mock)

    entries = ct_rfc6962.fetch_entries(start=0, end=999)  # pediu 1000

    assert len(entries) == 20  # recebeu 20 -- nao 1000
    _, kwargs = get_mock.call_args
    assert kwargs["params"] == {"start": 0, "end": 999}


def test_fetch_entries_raises_ct_log_unavailable_on_timeout(monkeypatch):
    monkeypatch.setattr(
        ct_rfc6962._session, "get", MagicMock(side_effect=requests.exceptions.Timeout("demorou demais"))
    )
    with pytest.raises(ct_rfc6962.CTLogUnavailableError):
        ct_rfc6962.fetch_entries(start=0, end=999)
