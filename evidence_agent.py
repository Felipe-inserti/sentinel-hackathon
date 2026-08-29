"""Camada 4 -- Agente de Coleta de Evidencias (evidence-collector).

Hoje o revisor humano decide olhando so o `reasoning` do LLM (fraco, e um
texto gerado, nao um fato verificavel). Este modulo monta um dossie de
evidencia VERIFICAVEL -- screenshot, DNS, hospedagem/ASN, certificado TLS,
RDAP, hashes por artefato e um hash raiz do proprio manifesto -- coletado
ANTES da aprovacao humana, para que a decisao seja informada por dado, nao
so por texto de modelo.

Determinístico, sem LLM: mantem a tese de token economy do projeto (custo
zero de token nesta camada, ver CLAUDE.md).

## Gatilho e contrato

Consome `investigation-completed` (subscription propria `sub-evidence`,
ver `config.settings.evidence_subscription_id` e `infra/`) e so processa
mensagens com `classification == "MALICIOUS"` -- mensagens `SAFE` sao
`ack`eadas e ignoradas (nao e erro, so nao se aplica a este agente).

Descoberta via Agent Registry (ver `registry.py`), igual `orchestrator.py`:
cada mensagem chama `registry.invoke_agent("evidence-collector", ...)`, que
resolve a versao `ACTIVE` publicada e valida o payload contra o
`input_schema`. O `EvidenceBundle` abaixo E a fonte do `output_schema`
publicado em `seed_registry.py` (`evidence-collector@2.0.0` -- ver esse
arquivo para o historico da divergencia com o `output_schema` reservado em
`1.0.0`, que so tinha 4 campos e nao comportava chain of custody nenhuma).

## Falha graciosa

Cada secao de coleta (screenshot, DNS, hospedagem, TLS, RDAP, HTTP) roda
isolada, nunca propaga excecao: se falhar, o campo correspondente fica
`None` e um `CollectionError` e anexado a `collection_errors`. Um site
fora do ar produz um `EvidenceBundle` parcial (`is_partial=True`), nunca
derruba o pipeline.

## Seguranca

- Regra CLAUDE.md #5 ("PII nunca e persistida... Firestore, GCS ou
  logs"): todo texto extraido (o HTML bruto) passa por `sanitizer.sanitize`
  ANTES de subir para o GCS -- a integridade que o hash prova e da versao
  ja sanitizada, nao do byte a byte original do site (tradeoff necessario:
  a regra 5 e inegociavel, nao ha excecao de "e evidencia forense" no
  CLAUDE.md). Screenshot e imagem -- nao da pra redigir PII em pixel por
  regex, entao a mitigacao aqui e a prescrita pelo pedido do sprint:
  DETECTAR campo de formulario preenchido e SINALIZAR no bundle
  (`form_fields_detected`), nao redigir.
- O agente nunca executa JavaScript vindo da pagina fora do sandbox do
  Playwright: nenhuma chamada usa `page.evaluate()` com string vinda do
  conteudo raspado -- deteccao de formulario usa `Locator.input_value()`,
  API estruturada do Playwright, nunca `eval` de string arbitraria.
- Bloqueio de navegacao para fora do dominio alvo via `page.route`: toda
  requisicao de NAVEGACAO (`Request.is_navigation_request()`) para um
  hostname diferente do dominio investigado (ou subdominio dele) e
  abortada -- cobre o requisito "nunca segue link para fora do dominio
  alvo". Deliberadamente NAO bloqueia sub-recursos (imagem/CSS/fonte) de
  outros hosts -- o pedido e sobre navegacao/links, e bloquear tambem
  sub-recursos quebraria a fidelidade visual do proprio screenshot que
  estamos coletando como evidencia.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import socket
import ssl
from datetime import datetime, timezone
from urllib.parse import urlparse

import dns.exception
import dns.resolver
import requests
from bs4 import BeautifulSoup
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from google.cloud import firestore, pubsub_v1, storage
from opentelemetry import context as otel_context
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from pydantic import BaseModel, Field

import registry
import telemetry
from config import settings
from sanitizer import sanitize

tracer = telemetry.setup("sentinel-evidence-collector")
logger = logging.getLogger("evidence_agent")

# Identidade deste processo no Agent Registry (ver registry.py) -- mesmo
# padrao de orchestrator.py: nenhuma versao/contrato hard-coded, cada
# mensagem resolve a versao ACTIVE atual via registry.invoke_agent.
AGENT_ID = "evidence-collector"

HTTP_TIMEOUT_SECONDS = 8
PLAYWRIGHT_TIMEOUT_MS = 15_000  # "timeout agressivo", pedido explicito do sprint
DNS_TIMEOUT_SECONDS = 5.0
MAX_HTML_BYTES = 5_000_000  # limite defensivo -- evidencia forense, nao prompt de LLM, entao bem mais folgado que MAX_SCRAPED_CHARS do orchestrator
MAX_INFLIGHT_MESSAGES = 5

DOSSIER_STATUS_PENDING_HUMAN_REVIEW = "PENDING_HUMAN_REVIEW"

db = firestore.Client()
storage_client = storage.Client()
subscriber = pubsub_v1.SubscriberClient()
subscription_path = subscriber.subscription_path(
    settings.gcp_project_id, settings.evidence_subscription_id
)

# Nomes de bucket GCS sao globalmente unicos -- mesma formula de
# infra/main.tf::local.evidence_bucket_name, so overridavel via
# settings.evidence_gcs_bucket se colidir com um bucket existente.
_EVIDENCE_BUCKET = settings.evidence_gcs_bucket or f"{settings.gcp_project_id}-sentinel-evidence"


# --- Modelos (fonte do output_schema publicado em seed_registry.py) --------


class ArtifactRef(BaseModel):
    """Um artefato (screenshot ou HTML) gravado no GCS, com o hash que
    prova que ele nao foi alterado depois da coleta (item 8)."""

    gcs_uri: str
    sha256: str
    content_type: str
    size_bytes: int


class HttpResponseSnapshot(BaseModel):
    """Resposta HTTP crua da primeira requisicao ao dominio (item 2) --
    nao e o HTML renderizado pelo Playwright, e o que o servidor devolveu
    de fato, incluindo a cadeia de redirects seguida ate chegar la."""

    status_code: int | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    redirect_chain: list[str] = Field(default_factory=list)
    final_url: str | None = None


class DnsRecords(BaseModel):
    a: list[str] = Field(default_factory=list)
    aaaa: list[str] = Field(default_factory=list)
    ns: list[str] = Field(default_factory=list)
    mx: list[str] = Field(default_factory=list)
    txt: list[str] = Field(default_factory=list)


class HostingInfo(BaseModel):
    ip_address: str | None = None
    asn: int | None = None
    asn_org: str | None = None


class TlsCertificateInfo(BaseModel):
    """Certificado FOLHA apresentado pelo servidor -- emissor, validade e
    SANs (item 5). Nao inclui a cadeia de intermediarias: extrair isso
    exigiria negociar TLS pedindo a cadeia completa e um parser mais
    pesado; o certificado folha ja responde a pergunta que mais importa
    pra triagem humana (quem emitiu, ha quanto tempo, pra quais nomes)."""

    issuer: str | None = None
    subject: str | None = None
    not_before: datetime | None = None
    not_after: datetime | None = None
    san: list[str] = Field(default_factory=list)


class RdapInfo(BaseModel):
    registrar: str | None = None
    domain_created_at: datetime | None = None
    domain_age_hours: float | None = None  # campo de destaque (item 6) -- dominio com poucas horas e sinal forte
    abuse_contacts: list[str] = Field(default_factory=list)


class InfrastructureFingerprint(BaseModel):
    """Hash combinado de sinais de infraestrutura, para agrupar (item 7)
    dominios diferentes gerados pelo mesmo kit/campanha de phishing."""

    html_template_hash: str | None = None  # estrutura DOM normalizada, sem conteudo
    ip_address: str | None = None
    asn: int | None = None
    registrar: str | None = None
    cert_issuer: str | None = None
    fingerprint_hash: str | None = None  # sha256 de todos os campos acima


class FormFieldSignal(BaseModel):
    """Sinal de pagina com formulario preenchido -- risco de PII de vitima
    capturada no screenshot (item 11). Deteccao, nao redacao: nao da pra
    redigir PII em pixel."""

    detected: bool = False
    field_count: int = 0


class CollectionError(BaseModel):
    """Uma etapa de coleta que falhou -- e isso que sustenta 'bundle
    parcial': cada falha fica registrada, nunca escondida."""

    step: str
    error: str


class EvidenceBundle(BaseModel):
    """Dossie de evidencia completo de um dominio MALICIOUS.

    `manifest_root_hash` = sha256 do dump JSON canonico (chaves ordenadas)
    do bundle inteiro COM este campo em branco -- para reverificar, zere
    `manifest_root_hash`, serialize com `sort_keys=True` e recalcule; se o
    hash bater, nada mudou desde a coleta (ver `_compute_root_hash`)."""

    domain: str
    collected_at: datetime  # UTC (item 9)

    screenshot: ArtifactRef | None = None
    html_snapshot: ArtifactRef | None = None
    http_response: HttpResponseSnapshot | None = None
    dns_records: DnsRecords | None = None
    hosting: HostingInfo | None = None
    tls_certificate: TlsCertificateInfo | None = None
    rdap: RdapInfo | None = None
    infrastructure_fingerprint: InfrastructureFingerprint | None = None

    pii_redacted: dict[str, int] = Field(default_factory=dict)
    form_fields_detected: FormFieldSignal = Field(default_factory=FormFieldSignal)

    collection_errors: list[CollectionError] = Field(default_factory=list)
    is_partial: bool = False

    manifest_root_hash: str = ""


# --- Utilitarios de hash/upload ---------------------------------------------


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _upload_artifact(
    bucket: storage.Bucket, domain: str, filename: str, data: bytes, content_type: str
) -> ArtifactRef:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    blob_path = f"{domain}/{timestamp}/{filename}"
    blob = bucket.blob(blob_path)
    blob.upload_from_string(data, content_type=content_type)
    return ArtifactRef(
        gcs_uri=f"gs://{bucket.name}/{blob_path}",
        sha256=_sha256_bytes(data),
        content_type=content_type,
        size_bytes=len(data),
    )


def _compute_root_hash(bundle: EvidenceBundle) -> str:
    payload = bundle.model_dump(mode="json")
    payload["manifest_root_hash"] = ""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- Item 2: HTML bruto + headers + redirects (requests, deterministico) ---


def _fetch_http_snapshot(url: str) -> tuple[HttpResponseSnapshot | None, str | None, CollectionError | None]:
    headers = {"User-Agent": "SentinelEvidenceCollector/1.0 (+security-research)"}
    try:
        response = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT_SECONDS, allow_redirects=True)
    except requests.exceptions.RequestException as exc:
        logger.warning("Falha ao buscar snapshot HTTP de %s: %s", url, exc)
        return None, None, CollectionError(step="http_snapshot", error=f"{exc.__class__.__name__}: {exc}"[:500])

    redirect_chain = [r.url for r in response.history] + [response.url]
    snapshot = HttpResponseSnapshot(
        status_code=response.status_code,
        headers=dict(response.headers),
        redirect_chain=redirect_chain,
        final_url=response.url,
    )
    # Corte por caracteres (aproximado, nao bytes exatos) -- limite defensivo
    # de tamanho, nao precisao de billing como MAX_SCRAPED_CHARS do orchestrator.
    html_text = response.text[:MAX_HTML_BYTES] if response.text else None
    return snapshot, html_text, None


# --- Item 3: DNS -------------------------------------------------------------


def _collect_dns(domain: str) -> tuple[DnsRecords, CollectionError | None]:
    resolver = dns.resolver.Resolver()
    resolver.timeout = DNS_TIMEOUT_SECONDS
    resolver.lifetime = DNS_TIMEOUT_SECONDS

    records = DnsRecords()
    field_by_qtype = {"A": "a", "AAAA": "aaaa", "NS": "ns", "MX": "mx", "TXT": "txt"}
    domain_resolves = False

    for qtype, field in field_by_qtype.items():
        try:
            answer = resolver.resolve(domain, qtype)
            setattr(records, field, [r.to_text().strip('"') for r in answer])
            domain_resolves = True
        except dns.resolver.NoAnswer:
            domain_resolves = True  # dominio existe, so nao tem esse tipo de registro -- nao e erro
        except (dns.resolver.NXDOMAIN, dns.resolver.NoNameservers, dns.exception.Timeout):
            continue
        except Exception:
            logger.exception("Falha inesperada resolvendo %s %s", qtype, domain)
            continue

    if not domain_resolves:
        return records, CollectionError(
            step="dns", error="Nenhum registro DNS resolvido (NXDOMAIN/timeout em todos os tipos consultados)"
        )
    return records, None


# --- Item 4: IP/ASN/org via Team Cymru (DNS TXT, sem API key) --------------
#
# Tecnica publica e amplamente documentada (nao um proxy de terceiro nao
# verificado): consulta TXT em "{ip-invertido}.origin.asn.cymru.com" devolve
# "ASN | prefixo | pais | RIR | data", e "AS{asn}.asn.cymru.com" devolve o
# nome da organizacao. Testado contra um IP real neste ambiente antes de
# escrever o parsing (nao adivinhado -- mesma disciplina do resto do
# projeto). So suporta IPv4 (esquema de IPv6 do Cymru exige reversao por
# nibble, mais complexo -- corte de escopo documentado, nao lacuna silenciosa).


def _collect_hosting(ip_address: str) -> tuple[HostingInfo | None, CollectionError | None]:
    resolver = dns.resolver.Resolver()
    resolver.timeout = DNS_TIMEOUT_SECONDS
    resolver.lifetime = DNS_TIMEOUT_SECONDS
    try:
        reversed_ip = ".".join(reversed(ip_address.split(".")))
        origin_answer = resolver.resolve(f"{reversed_ip}.origin.asn.cymru.com", "TXT")
        origin_parts = [p.strip() for p in origin_answer[0].to_text().strip('"').split("|")]
        asn = int(origin_parts[0]) if origin_parts and origin_parts[0].isdigit() else None

        asn_org = None
        if asn is not None:
            name_answer = resolver.resolve(f"AS{asn}.asn.cymru.com", "TXT")
            name_parts = [p.strip() for p in name_answer[0].to_text().strip('"').split("|")]
            asn_org = name_parts[-1] if name_parts else None

        return HostingInfo(ip_address=ip_address, asn=asn, asn_org=asn_org), None
    except Exception as exc:
        logger.warning("Falha ao consultar ASN (Team Cymru) para IP %s: %s", ip_address, exc)
        return None, CollectionError(step="hosting", error=f"{exc.__class__.__name__}: {exc}"[:500])


# --- Item 5: certificado TLS (stdlib ssl + cryptography) -------------------


def _collect_tls_certificate(domain: str) -> tuple[TlsCertificateInfo | None, CollectionError | None]:
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        # Sites de phishing frequentemente tem certificado invalido/vencido/
        # self-signed -- queremos INSPECIONAR mesmo assim, nao validar (a
        # validade em si e um dos sinais que vai pro dossie).
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((domain, 443), timeout=HTTP_TIMEOUT_SECONDS) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as ssock:
                der_cert = ssock.getpeercert(binary_form=True)

        if der_cert is None:
            return None, CollectionError(step="tls_certificate", error="Servidor nao apresentou certificado")

        cert = x509.load_der_x509_certificate(der_cert, default_backend())
        try:
            sans = cert.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value.get_values_for_type(x509.DNSName)
        except x509.ExtensionNotFound:
            sans = []

        return (
            TlsCertificateInfo(
                issuer=cert.issuer.rfc4514_string(),
                subject=cert.subject.rfc4514_string(),
                not_before=cert.not_valid_before_utc,
                not_after=cert.not_valid_after_utc,
                san=sans,
            ),
            None,
        )
    except Exception as exc:
        logger.warning("Falha ao coletar certificado TLS de %s: %s", domain, exc)
        return None, CollectionError(step="tls_certificate", error=f"{exc.__class__.__name__}: {exc}"[:500])


# --- Item 6: RDAP (RFC 7484 bootstrap da IANA + RFC 9083 JSON) -------------
#
# Bootstrap e parsing testados contra um dominio real (rdap.verisign.com)
# neste ambiente antes de escrever o codigo -- formato de "services",
# "entities"/"roles"/"vcardArray" e "events"/"eventAction"/"eventDate"
# confirmado, nao adivinhado.

_IANA_DNS_BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"


def _rdap_domain_base_url(domain: str) -> str | None:
    try:
        resp = requests.get(_IANA_DNS_BOOTSTRAP_URL, timeout=HTTP_TIMEOUT_SECONDS)
        resp.raise_for_status()
        bootstrap = resp.json()
    except Exception:
        logger.exception("Falha ao buscar bootstrap RDAP da IANA")
        return None

    tld = domain.rsplit(".", 1)[-1].lower()
    for tlds, urls in bootstrap.get("services", []):
        if tld in [t.lower() for t in tlds] and urls:
            return urls[0].rstrip("/")
    return None


def _extract_vcard_field(vcard_array: list | None, field_name: str) -> str | None:
    if not vcard_array or len(vcard_array) < 2:
        return None
    for field in vcard_array[1]:
        if field[0] == field_name and len(field) > 3 and field[3]:
            return field[3]
    return None


def _collect_rdap_domain(domain: str) -> tuple[RdapInfo | None, CollectionError | None]:
    base_url = _rdap_domain_base_url(domain)
    if base_url is None:
        return None, CollectionError(step="rdap", error=f"Nenhum servidor RDAP encontrado para o TLD de {domain}")

    try:
        resp = requests.get(
            f"{base_url}/domain/{domain}",
            timeout=HTTP_TIMEOUT_SECONDS,
            headers={"Accept": "application/rdap+json"},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Falha na consulta RDAP para %s: %s", domain, exc)
        return None, CollectionError(step="rdap", error=f"{exc.__class__.__name__}: {exc}"[:500])

    registrar = None
    abuse_contacts: list[str] = []
    for entity in data.get("entities", []):
        roles = entity.get("roles", [])
        if "registrar" in roles:
            name = _extract_vcard_field(entity.get("vcardArray"), "fn")
            if name:
                registrar = name
        for sub_entity in entity.get("entities", []) + ([entity] if "abuse" in roles else []):
            if "abuse" in sub_entity.get("roles", []):
                email = _extract_vcard_field(sub_entity.get("vcardArray"), "email")
                if email:
                    abuse_contacts.append(email)

    created_at = None
    for event in data.get("events", []):
        if event.get("eventAction") == "registration":
            try:
                created_at = datetime.fromisoformat(event["eventDate"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                pass

    domain_age_hours = None
    if created_at is not None:
        domain_age_hours = (datetime.now(timezone.utc) - created_at).total_seconds() / 3600

    return (
        RdapInfo(
            registrar=registrar,
            domain_created_at=created_at,
            domain_age_hours=domain_age_hours,
            abuse_contacts=abuse_contacts,
        ),
        None,
    )


# --- Item 7: fingerprint de infraestrutura ----------------------------------


def _normalize_dom_structure(html: str) -> str:
    """So a sequencia de tags (sem texto/atributos) -- normaliza a
    ESTRUTURA do template, ignorando conteudo que varia por vitima/
    campanha, pra agrupar paginas do mesmo kit de phishing."""
    soup = BeautifulSoup(html, "html.parser")
    return ">".join(tag.name for tag in soup.find_all(True))


def _compute_fingerprint(
    html_text: str | None,
    hosting: HostingInfo | None,
    tls_certificate: TlsCertificateInfo | None,
    rdap: RdapInfo | None,
) -> InfrastructureFingerprint:
    html_template_hash = None
    if html_text:
        html_template_hash = hashlib.sha256(_normalize_dom_structure(html_text).encode("utf-8")).hexdigest()

    ip_address = hosting.ip_address if hosting else None
    asn = hosting.asn if hosting else None
    registrar = rdap.registrar if rdap else None
    cert_issuer = tls_certificate.issuer if tls_certificate else None

    combined = "|".join(str(v) for v in (html_template_hash, ip_address, asn, registrar, cert_issuer))
    fingerprint_hash = hashlib.sha256(combined.encode("utf-8")).hexdigest()

    return InfrastructureFingerprint(
        html_template_hash=html_template_hash,
        ip_address=ip_address,
        asn=asn,
        registrar=registrar,
        cert_issuer=cert_issuer,
        fingerprint_hash=fingerprint_hash,
    )


# --- Itens 1, 11, 12: screenshot via Playwright (sandboxed, dominio travado) -


async def _domain_lock_router(route, request, target_domain: str) -> None:
    """So intercepta NAVEGACAO (documento/iframe) -- sub-recursos (imagem/
    CSS/fonte) de outros hosts continuam liberados, senao o screenshot sai
    quebrado visualmente. Ver docstring do modulo."""
    if request.is_navigation_request():
        hostname = urlparse(request.url).hostname or ""
        if hostname != target_domain and not hostname.endswith(f".{target_domain}"):
            logger.warning("Navegacao bloqueada por sair do dominio alvo (%s): %s", target_domain, request.url)
            await route.abort()
            return
    await route.continue_()


async def _detect_filled_form_fields(page) -> FormFieldSignal:
    """API estruturada do Playwright (`Locator.input_value`), nunca
    `page.evaluate` com string arbitraria -- ver regra de seguranca #12."""
    try:
        elements = await page.locator("input, textarea, select").all()
    except PlaywrightError:
        return FormFieldSignal()

    filled = 0
    for element in elements:
        try:
            value = await element.input_value(timeout=1000)
        except PlaywrightError:
            continue
        if value and value.strip():
            filled += 1

    return FormFieldSignal(detected=filled > 0, field_count=filled)


def _target_url(domain: str) -> str:
    """`https://{domain}` sempre, EXCETO com `settings.demo_insecure_http`
    ligado (opt-in explicito, default False, nunca setado em producao) --
    mesma logica de `plane2_agents.orchestrator._target_url`, duplicada
    aqui de proposito (2 linhas, sem justificar um modulo compartilhado
    novo -- ver CLAUDE.md sobre abstracao especulativa). Usado so para
    apontar a gravacao de demo a um `python -m http.server` local. Ver
    config.py e docs/DEMO_COMMANDS.md."""
    if settings.demo_insecure_http:
        return f"http://{domain}:{settings.demo_local_http_port}"
    return f"https://{domain}"


async def _capture_screenshot_and_form_signal(
    domain: str,
) -> tuple[bytes | None, FormFieldSignal, CollectionError | None]:
    url = _target_url(domain)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, timeout=PLAYWRIGHT_TIMEOUT_MS)
            try:
                context = await browser.new_context(ignore_https_errors=True)
                page = await context.new_page()
                await page.route("**/*", lambda route, request: _domain_lock_router(route, request, domain))
                await page.goto(url, timeout=PLAYWRIGHT_TIMEOUT_MS, wait_until="domcontentloaded")

                form_signal = await _detect_filled_form_fields(page)
                screenshot_bytes = await page.screenshot(full_page=True, timeout=PLAYWRIGHT_TIMEOUT_MS)
                return screenshot_bytes, form_signal, None
            finally:
                await browser.close()
    except (PlaywrightError, PlaywrightTimeoutError) as exc:
        logger.warning("Falha ao capturar screenshot de %s: %s", domain, exc)
        return None, FormFieldSignal(), CollectionError(step="screenshot", error=str(exc)[:500])
    except Exception as exc:  # defesa extra -- coleta de evidencia nunca derruba o pipeline
        logger.exception("Erro inesperado capturando screenshot de %s", domain)
        return None, FormFieldSignal(), CollectionError(step="screenshot", error=f"{exc.__class__.__name__}: {exc}"[:500])


# --- Orquestracao da coleta --------------------------------------------------


async def collect_evidence(domain: str) -> EvidenceBundle:
    """Roda todas as secoes de coleta, cada uma isolada (falha de uma nao
    derruba as outras -- ver docstring do modulo), monta o `EvidenceBundle`
    e fecha com o hash raiz. Nao carimba `agent_id`/`agent_version` -- isso
    e feito por quem persiste (`_update_dossier_with_evidence`), mesmo
    padrao de `orchestrator.AnalysisResult`."""
    with tracer.start_as_current_span("evidence.collect") as span:
        span.set_attribute("evidence.domain", domain)
        errors: list[CollectionError] = []

        with tracer.start_as_current_span("evidence.screenshot"):
            screenshot_bytes, form_signal, err = await _capture_screenshot_and_form_signal(domain)
        if err:
            errors.append(err)

        with tracer.start_as_current_span("evidence.http_snapshot"):
            http_response, html_text_raw, err = await asyncio.to_thread(_fetch_http_snapshot, _target_url(domain))
        if err:
            errors.append(err)

        # Regra CLAUDE.md #5: sanitiza ANTES de qualquer persistencia (GCS
        # incluso) -- ver docstring do modulo sobre o tradeoff de hash.
        pii_redacted: dict[str, int] = {}
        html_sanitized_text: str | None = None
        if html_text_raw is not None:
            sanitized = sanitize(html_text_raw)
            html_sanitized_text = sanitized.clean_text
            pii_redacted = sanitized.pii_redacted

        with tracer.start_as_current_span("evidence.dns"):
            dns_records, err = await asyncio.to_thread(_collect_dns, domain)
        if err:
            errors.append(err)

        hosting = None
        ip_address = dns_records.a[0] if dns_records.a else None
        if ip_address:
            with tracer.start_as_current_span("evidence.hosting"):
                hosting, err = await asyncio.to_thread(_collect_hosting, ip_address)
            if err:
                errors.append(err)
        else:
            errors.append(
                CollectionError(step="hosting", error="Sem registro A -- nao foi possivel resolver IP de hospedagem")
            )

        with tracer.start_as_current_span("evidence.tls_certificate"):
            tls_certificate, err = await asyncio.to_thread(_collect_tls_certificate, domain)
        if err:
            errors.append(err)

        with tracer.start_as_current_span("evidence.rdap"):
            rdap, err = await asyncio.to_thread(_collect_rdap_domain, domain)
        if err:
            errors.append(err)

        fingerprint = _compute_fingerprint(html_sanitized_text, hosting, tls_certificate, rdap)

        bucket = storage_client.bucket(_EVIDENCE_BUCKET)
        screenshot_ref = None
        if screenshot_bytes is not None:
            screenshot_ref = await asyncio.to_thread(
                _upload_artifact, bucket, domain, "screenshot.png", screenshot_bytes, "image/png"
            )
        html_ref = None
        if html_sanitized_text is not None:
            html_ref = await asyncio.to_thread(
                _upload_artifact,
                bucket,
                domain,
                "html_snapshot.html",
                html_sanitized_text.encode("utf-8"),
                "text/html; charset=utf-8",
            )

        bundle = EvidenceBundle(
            domain=domain,
            collected_at=datetime.now(timezone.utc),
            screenshot=screenshot_ref,
            html_snapshot=html_ref,
            http_response=http_response,
            dns_records=dns_records,
            hosting=hosting,
            tls_certificate=tls_certificate,
            rdap=rdap,
            infrastructure_fingerprint=fingerprint,
            pii_redacted=pii_redacted,
            form_fields_detected=form_signal,
            collection_errors=errors,
            is_partial=bool(errors),
        )
        bundle = bundle.model_copy(update={"manifest_root_hash": _compute_root_hash(bundle)})

        span.set_attribute("evidence.is_partial", bundle.is_partial)
        span.set_attribute("evidence.collection_errors", [e.step for e in errors])
        span.set_attribute("evidence.form_fields_detected", form_signal.detected)

        telemetry.increment_counter("evidence_bundles_collected_total")
        deltas = {"evidence_bundles_collected_total": 1}
        if bundle.is_partial:
            telemetry.increment_counter("evidence_bundles_partial_total")
            deltas["evidence_bundles_partial_total"] = 1
        await asyncio.to_thread(telemetry.flush_metrics_to_firestore, deltas)

        return bundle


def _update_dossier_with_evidence(domain: str, bundle: EvidenceBundle, agent_manifest: registry.AgentManifest) -> None:
    """Atualiza (nao sobrescreve) o dossie ja gravado por
    `orchestrator._save_investigation` em `investigations/{domain}`.
    `merge=True`: cria o documento se por algum motivo ainda nao existir
    (ex: reprocessamento manual da mensagem) sem apagar campos ja gravados
    pela investigacao -- mesmo espirito de `telemetry.flush_metrics_to_firestore`."""
    doc_ref = db.collection(settings.firestore_collection).document(domain)
    doc_ref.set(
        {
            "evidence": bundle.model_dump(mode="json"),
            "status": DOSSIER_STATUS_PENDING_HUMAN_REVIEW,
            "evidence_agent_id": agent_manifest.agent_id,
            "evidence_agent_version": agent_manifest.version,
        },
        merge=True,
    )


# --- Consumo do Pub/Sub -------------------------------------------------


def _handle_pubsub_message(message: pubsub_v1.subscriber.message.Message, loop: asyncio.AbstractEventLoop) -> None:
    try:
        payload = json.loads(message.data.decode("utf-8"))
    except json.JSONDecodeError:
        logger.exception("JSON invalido recebido, descartando (nack)")
        message.nack()
        return

    with tracer.start_as_current_span("registry.invoke") as span:
        span.set_attribute("registry.agent_id", AGENT_ID)
        try:
            agent_manifest = registry.invoke_agent(AGENT_ID, payload)
        except (registry.AgentNotFoundError, registry.AgentInvocationError) as exc:
            span.set_attribute("registry.rejected", True)
            logger.error("Mensagem rejeitada pelo Agent Registry: %s", exc)
            message.nack()
            return
        span.set_attribute("registry.rejected", False)
        span.set_attribute("registry.agent_version", agent_manifest.version)

    if payload.get("classification") != "MALICIOUS":
        logger.info(
            "Ignorando %s: classification=%s (evidence-collector so coleta para MALICIOUS)",
            payload.get("domain"),
            payload.get("classification"),
        )
        message.ack()
        return

    domain = payload["domain"]
    extracted_ctx = telemetry.extract_context(message.attributes)

    async def _process() -> None:
        token = otel_context.attach(extracted_ctx)
        try:
            bundle = await collect_evidence(domain)
            await asyncio.to_thread(_update_dossier_with_evidence, domain, bundle, agent_manifest)
            message.ack()
        except Exception:
            logger.exception("Falha ao coletar evidencia para %s", domain)
            message.nack()
        finally:
            otel_context.detach(token)

    asyncio.run_coroutine_threadsafe(_process(), loop)


async def run_evidence_collector() -> None:
    loop = asyncio.get_running_loop()
    flow_control = pubsub_v1.types.FlowControl(max_messages=MAX_INFLIGHT_MESSAGES)

    streaming_pull_future = subscriber.subscribe(
        subscription_path,
        callback=lambda message: _handle_pubsub_message(message, loop),
        flow_control=flow_control,
    )
    logger.info("Evidence collector escutando em %s", subscription_path)

    try:
        await asyncio.to_thread(streaming_pull_future.result)
    except asyncio.CancelledError:
        streaming_pull_future.cancel()
        raise
    except Exception:
        logger.exception("Stream de Pub/Sub encerrado com erro")
        streaming_pull_future.cancel()
        raise


if __name__ == "__main__":
    try:
        asyncio.run(run_evidence_collector())
    except KeyboardInterrupt:
        logger.info("Encerrado pelo usuario")
