"""Leitura do Certificate Transparency via polling RFC 6962 (get-sth /
get-entries) -- substitui o websocket do certstream (fora do ar, sem
replay, ver git log). ISOLADO de proposito: este modulo so sabe falar HTTP
com UM log RFC 6962 e decodificar o formato binario de cada folha; nao
conhece prefiltro, Gemma, Pub/Sub nem Firestore. `ct_listener.py` e o unico
consumidor.

## Por que um modulo separado

O parsing de `leaf_input`/`extra_data` e byte a byte (RFC 6962 SS3.4) e tem
uma ramificacao facil de errar: `x509_entry` traz o certificado completo
dentro do proprio `leaf_input`; `precert_entry` NAO -- o `leaf_input` so
tem o hash da chave do emissor mais o `TBSCertificate` (sem assinatura, sem
o wrapper `Certificate` que o `cryptography` exige para carregar). O
certificado completo do precert (com a extensao *poison*, mas
perfeitamente parseavel como DER porque foi assinado de verdade pela CA)
so existe no `extra_data` (`PrecertChainEntry.pre_certificate`). Isolar essa
logica aqui, coberta por teste unitario com bytes reais dos dois formatos,
evita que esse detalhe se perca no meio do loop de polling.

## Fonte normativa (verificado por leitura direta da RFC, nao por memoria)

RFC 6962 SS3.4 (MerkleTreeLeaf) e SS4.6 (get-entries), rfc-editor.org/rfc/rfc6962.html:

    struct {
        Version version;        // 1 byte
        MerkleLeafType leaf_type;  // 1 byte -- 0 = timestamped_entry
        select (leaf_type) { case timestamped_entry: TimestampedEntry; }
    } MerkleTreeLeaf;

    struct {
        uint64 timestamp;        // 8 bytes, ms desde epoch
        LogEntryType entry_type; // 2 bytes -- 0 = x509_entry, 1 = precert_entry
        select(entry_type) {
            case x509_entry: ASN.1Cert;   // opaque<1..2^24-1> -- 3 bytes de tamanho + DER
            case precert_entry: PreCert;  // ver abaixo
        } signed_entry;
        CtExtensions extensions;
    } TimestampedEntry;

    struct {
        opaque issuer_key_hash[32];
        opaque TBSCertificate<1..2^24-1>;  // 3 bytes de tamanho + DER (SEM assinatura)
    } PreCert;

    // extra_data quando entry_type == precert_entry:
    struct {
        ASN.1Cert pre_certificate;              // 3 bytes de tamanho + DER completo
        ASN.1Cert precertificate_chain<0..2^24-1>;
    } PrecertChainEntry;

"Logs MAY restrict the number of entries that can be retrieved per
get-entries request. If a client requests more than the permitted number
of entries, the log SHALL return the maximum number of entries
permissible." -- confirmado ao vivo contra Argon2026h2: pedidos de
1000/2000 entradas devolveram 20/32. Por isso `fetch_entries` devolve
exatamente o que o log mandou, e quem chama (`ct_listener.py`) avanca o
cursor pelo `len()` da resposta, nunca pelo tamanho pedido.
"""

from __future__ import annotations

import base64
import datetime
import logging
import struct
import time
from typing import Any, Literal

import requests
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from pydantic import BaseModel

from config import settings

logger = logging.getLogger("ct_rfc6962")

# leaf_type / entry_type sao campos numericos de largura fixa do protocolo
# (RFC 6962 SS3.4) -- constantes, nao configuracao.
_LEAF_TYPE_TIMESTAMPED_ENTRY = 0
_ENTRY_TYPE_X509 = 0
_ENTRY_TYPE_PRECERT = 1


class CTLogUnavailableError(Exception):
    """Erro HTTP transitorio (timeout, 5xx, conexao) falando com o log --
    `ct_listener.py` trata isso com backoff exponencial, nunca deixa a
    excecao crua matar o processo."""


class CTLogRateLimitedError(CTLogUnavailableError):
    """Subclasse especifica para HTTP 429 -- ainda tratado com backoff
    (todo `except CTLogUnavailableError` continua pegando isto tambem),
    mas o controlador de concorrencia da ingestao paralela (ver
    `ct_listener.py::_ConcurrencyController`) precisa distinguir "429 de
    verdade" de timeout/5xx generico para saber QUANDO parar de subir a
    concorrencia e reduzir -- pedido explicito: "suba gradualmente e PARE
    no primeiro 429", nao num erro transitorio qualquer."""


class SignedTreeHead(BaseModel):
    """Subconjunto de get-sth (RFC 6962 SS4.3) que de fato usamos --
    so o tamanho da arvore, para saber ate onde da para ler."""

    tree_size: int
    timestamp: int


class ParsedCertEntry(BaseModel):
    """Saida do parsing de UMA entrada do log -- o unico contrato que
    `ct_listener.py` conhece deste modulo. Formato deliberadamente
    equivalente ao que o certstream entregava antes (`domains` +
    `certificate_age_seconds`), para que o pipeline downstream (prefiltro,
    Gemma, orquestrador) nao precise mudar uma linha."""

    log_index: int
    entry_type: Literal["x509_entry", "precert_entry"]
    domains: list[str]
    certificate_age_seconds: float | None


_session = requests.Session()


def _base_url() -> str:
    return settings.ct_log_base_url.rstrip("/")


def _raise_for_transient_error(exc: requests.exceptions.RequestException, context: str) -> None:
    """Ponto UNICO de decisao "isso e 429 ou e outro erro transitorio?" --
    usado por `fetch_sth` e `fetch_entries`, para as duas chamadas
    classificarem erro do MESMO jeito. `exc.response` so existe em
    `HTTPError` (levantado por `raise_for_status()`); erros de conexao/
    timeout puros (`ConnectionError`/`Timeout`) nao tem resposta nenhuma,
    entao caem direto no caso generico."""
    response = getattr(exc, "response", None)
    if response is not None and response.status_code == 429:
        raise CTLogRateLimitedError(f"{context}: 429 Too Many Requests") from exc
    raise CTLogUnavailableError(f"{context} falhou: {exc}") from exc


def fetch_sth() -> SignedTreeHead:
    """GET .../ct/v1/get-sth -- devolve o tamanho atual da arvore. Levanta
    `CTLogRateLimitedError` especificamente em 429, `CTLogUnavailableError`
    em qualquer outra falha de rede/HTTP; nunca deixa `requests.exceptions.*`
    cru escapar para quem chama."""
    url = f"{_base_url()}/ct/v1/get-sth"
    try:
        response = _session.get(url, timeout=settings.ct_http_timeout_seconds)
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        _raise_for_transient_error(exc, "get-sth")
    return SignedTreeHead.model_validate(response.json())


def fetch_entries(start: int, end: int) -> list[dict[str, Any]]:
    """GET .../ct/v1/get-entries?start=X&end=Y -- devolve a lista crua de
    `{leaf_input, extra_data}` (base64), SEM decodificar. O log pode (e vai,
    ver docstring do modulo) devolver menos entradas do que o intervalo
    pedido; quem chama precisa usar `len(resultado)` para saber quanto
    realmente avancou, nunca `end - start + 1`. Levanta
    `CTLogRateLimitedError` especificamente em 429 (ver
    `_raise_for_transient_error`)."""
    url = f"{_base_url()}/ct/v1/get-entries"
    try:
        response = _session.get(
            url, params={"start": start, "end": end}, timeout=settings.ct_http_timeout_seconds
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        _raise_for_transient_error(exc, f"get-entries({start}, {end})")
    return response.json().get("entries", [])


def _read_opaque_vector(data: bytes, offset: int, length_prefix_bytes: int) -> bytes:
    """Le um vetor opaco estilo TLS (RFC 6962/RFC 5246 SS4.6): um prefixo de
    tamanho big-endian de `length_prefix_bytes` bytes, seguido do conteudo.
    Devolve so o conteudo (o offset de continuacao nao importa aqui --
    cada chamador so precisa do PRIMEIRO campo variavel da struct)."""
    length = int.from_bytes(data[offset : offset + length_prefix_bytes], "big")
    start = offset + length_prefix_bytes
    return data[start : start + length]


def _parse_domains_and_not_before(der_cert: bytes) -> tuple[list[str], datetime.datetime]:
    """Carrega o certificado DER com `cryptography` e devolve (domains do
    SubjectAlternativeName, not_valid_before) -- mesma API ja usada em
    `evidence_agent.py` (parse da cadeia TLS), nao um segundo jeito de
    fazer a mesma coisa. Filtra `*.*` (mesmo guard que `_extract_domains`
    tinha no certstream, para entradas com SAN mal-formado)."""
    cert = x509.load_der_x509_certificate(der_cert, default_backend())
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        domains = san.value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        domains = []
    filtered = [d for d in domains if d and not d.startswith("*.*")]
    return filtered, cert.not_valid_before_utc


def parse_leaf_entry(log_index: int, leaf_input_b64: str, extra_data_b64: str) -> ParsedCertEntry | None:
    """Decodifica UMA entrada de get-entries. Devolve `None` (nunca
    levanta) para entradas que nao servem para o pipeline: leaf_type
    desconhecido, entry_type desconhecido, DER corrompido, ou SAN vazio --
    o chamador so precisa filtrar `None` e seguir, igual ao filtro que ja
    existia em `_extract_domains` do certstream."""
    try:
        leaf = base64.b64decode(leaf_input_b64)
    except (ValueError, TypeError):
        logger.warning("leaf_input invalido (nao decodifica base64) no indice %d", log_index)
        return None

    if len(leaf) < 12:
        logger.warning("leaf_input curto demais (%d bytes) no indice %d", len(leaf), log_index)
        return None

    leaf_type = leaf[1]
    if leaf_type != _LEAF_TYPE_TIMESTAMPED_ENTRY:
        logger.warning("leaf_type %d inesperado no indice %d, ignorando", leaf_type, log_index)
        return None

    entry_type_code = struct.unpack(">H", leaf[10:12])[0]

    try:
        if entry_type_code == _ENTRY_TYPE_X509:
            # x509_entry: o certificado completo ja esta no leaf_input,
            # logo apos timestamp(8) + entry_type(2) -- offset 12.
            der_cert = _read_opaque_vector(leaf, 12, 3)
            entry_type: Literal["x509_entry", "precert_entry"] = "x509_entry"
        elif entry_type_code == _ENTRY_TYPE_PRECERT:
            # precert_entry: leaf_input SO tem issuer_key_hash+TBSCertificate
            # (sem assinatura -- nao carrega no cryptography). O certificado
            # completo (com poison extension, mas assinado de verdade pela
            # CA) esta em extra_data.pre_certificate.
            if not extra_data_b64:
                logger.warning("precert_entry sem extra_data no indice %d", log_index)
                return None
            extra = base64.b64decode(extra_data_b64)
            der_cert = _read_opaque_vector(extra, 0, 3)
            entry_type = "precert_entry"
        else:
            logger.warning("entry_type %d desconhecido no indice %d, ignorando", entry_type_code, log_index)
            return None
    except (ValueError, TypeError, IndexError):
        logger.warning("falha ao extrair DER (entry_type=%d) no indice %d", entry_type_code, log_index)
        return None

    try:
        domains, not_before = _parse_domains_and_not_before(der_cert)
    except Exception:
        logger.warning("falha ao parsear certificado DER no indice %d", log_index, exc_info=True)
        return None

    if not domains:
        return None

    certificate_age_seconds = max(time.time() - not_before.timestamp(), 0.0)

    return ParsedCertEntry(
        log_index=log_index,
        entry_type=entry_type,
        domains=domains,
        certificate_age_seconds=certificate_age_seconds,
    )
