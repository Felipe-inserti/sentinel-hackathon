"""Sanitizador de conteudo raspado -- defesa em profundidade entre o
scraper e o LLM.

O texto extraido de um site suspeito e adversarial por definicao (ver
CLAUDE.md): pode conter prompt injection tentando forcar uma classificacao
SAFE, e pode conter PII de vitimas (CPF, cartao, credenciais) que nunca
pode ser persistida. Este modulo nao depende de `config`/GCP -- e stdlib +
Pydantic puros, testavel sem nenhuma variavel de ambiente.

Pipeline de `sanitize()`: NFKC -> remove categorias Unicode Cf/Co/Cs (cobre
Unicode Tag Characters U+E0001-U+E007F, zero-width space/joiners, BOM e
overrides/isolates bidirecionais -- todos categoria Cf, um unico filtro
por caractere resolve tudo) -> colapso de whitespace -> deteccao de
injecao por regex (PT+EN, redige e registra) -> deteccao de PII brasileira
(cartao/CNPJ/CPF/telefone/e-mail, redige e conta por tipo).

Isolamento estrutural (`wrap_untrusted_content`) gera um nonce aleatorio
por requisicao e embrulha o texto sanitizado em delimitadores que o
contem -- se esse nonce reaparecer no proprio texto sanitizado, e um sinal
quase impossivel de ocorrer por acaso (128 bits de entropia) e tratado
como tentativa de escape do delimitador.

## Model Armor (Google Cloud) -- pesquisa registrada por requisito

Investiguei o uso do Model Armor (SanitizeUserPrompt) como camada adicional
antes do modelo:
  - Regiao: `us-central1` (Iowa) E suportada -- confirmado via fetch direto
    de https://docs.cloud.google.com/model-armor/locations (nao adivinhado).
    Compativel com o default de `config.gcp_location`.
  - Pacote Python: `google-cloud-modelarmor`
    (`pip install --upgrade google-cloud-modelarmor`). O nome exato da
    classe cliente e a assinatura do metodo NAO foram confirmados na
    documentacao disponivel -- exigiria instalar o pacote de verdade e
    inspecionar (mesma disciplina usada para `google-genai`: nunca
    adivinhar nome de metodo/classe).
  - A API opera sobre um recurso `projects/*/locations/*/templates/*` --
    ou seja, exige provisionar um "template" via gcloud/console/Terraform
    ANTES de poder chamar `sanitizeUserPrompt`. E uma dependencia de
    infraestrutura nova (como os topicos Pub/Sub), nao so uma chamada SDK.
  - Decisao: NAO integrado agora. Nao consigo testar contra um projeto GCP
    real neste ambiente, e o CLAUDE.md proibe abstracao especulativa --
    criar um modulo/flag de config para uma integracao cuja API nem pude
    confirmar seria exatamente isso. O sanitizer.py + `requires_human_review`
    obrigatorio no orchestrator ja formam a rede de seguranca real. Se um
    projeto GCP com Model Armor habilitado ficar disponivel, o proximo
    passo e: `pip install google-cloud-modelarmor`, inspecionar o client
    real, provisionar um template com `gcloud model-armor templates create`,
    so entao adicionar a chamada aqui.
"""

from __future__ import annotations

import re
import secrets
import unicodedata

from pydantic import BaseModel, Field


class SanitizationResult(BaseModel):
    clean_text: str
    injection_patterns_found: list[str] = Field(default_factory=list)
    pii_redacted: dict[str, int] = Field(default_factory=dict)
    delimiter_escape_attempted: bool = False


class IsolatedPrompt(BaseModel):
    nonce: str
    wrapped_content: str
    sanitized: SanitizationResult


# --- Normalizacao ------------------------------------------------------

_STRIP_CATEGORIES = frozenset({"Cf", "Co", "Cs"})


def _strip_invisible_and_private(text: str) -> str:
    """Remove caracteres de formatacao invisivel (Cf -- inclui Unicode Tag
    Characters U+E0001-U+E007F, zero-width space/joiners, BOM e
    overrides/isolates bidirecionais), uso privado (Co) e surrogates soltos
    (Cs). E destrutivo, nao decodificador -- um payload inteiro escondido
    via Tag Characters e apagado, nunca revelado como texto visivel."""
    return "".join(ch for ch in text if unicodedata.category(ch) not in _STRIP_CATEGORIES)


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# --- Fold de homoglifos (so para deteccao de injecao, letras apenas) ---
# Tabela propria, distinta do _HOMOGLYPH_MAP privado de
# plane1_ingestion/prefilter.py (aquele inclui digitos de leetspeak
# afinados para nomes de dominio curtos -- aplicar isso a texto de pagina
# inteira corromperia precos/telefones legitimos antes da deteccao de PII).
_HOMOGLYPH_FOLD_MAP: dict[str, str] = {
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c",
    "х": "x", "у": "y", "і": "i", "ј": "j", "ѕ": "s", "ⅼ": "l",
}
_HOMOGLYPH_FOLD_TABLE = str.maketrans(_HOMOGLYPH_FOLD_MAP)


# --- Deteccao de injecao -------------------------------------------------

_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ignore_previous_instructions", re.compile(
        r"\b(ignor[ae]|desconsider[ae])\b.{0,30}\b(instru[cç][oõ]es?)\b.{0,20}\b(anterior(es)?|acima)\b"
        r"|\bignore\b.{0,20}\b(previous|above|prior)\b.{0,10}\binstructions?\b",
        re.IGNORECASE,
    )),
    ("role_redefinition", re.compile(
        r"\byou\s+are\s+now\b"
        r"|\bvoc[eê]\s+agora\s+(é|e|atua\s+como|se\s+torna|deve\s+agir\s+como)\b",
        re.IGNORECASE,
    )),
    ("system_prompt_mention", re.compile(
        r"\bsystem\s*prompt\b|\bprompt\s+do\s+sistema\b|\binstru[cç][oõ]es?\s+de\s+sistema\b",
        re.IGNORECASE,
    )),
    ("explicit_safe_request", re.compile(
        r"\b(classifiqu[ei]|classify|marque|mark|responda|reply|retorne|return|output|diga|say)\b"
        r".{0,25}\b(como\s+)?(safe|seguro)\b",
        re.IGNORECASE,
    )),
    ("chat_control_tags", re.compile(
        r"\[/?INST\]|<\|.*?\|>|</?system>",
        re.IGNORECASE,
    )),
    ("simulated_structured_response", re.compile(
        r"```(?:json)?\s*\{[^`]{0,200}\b(classification|MALICIOUS|SAFE)\b[^`]{0,200}\}\s*```",
        re.IGNORECASE | re.DOTALL,
    )),
]


def _redact_matches(text: str, folded: str, pattern: re.Pattern[str]) -> tuple[str, bool]:
    """Acha spans em `folded` (variante com homoglifos traduzidos, mesmo
    comprimento/offsets de `text` pois o fold e 1:1 caractere-a-caractere)
    e substitui os spans equivalentes em `text` por `[REDACTED]`."""
    spans = [m.span() for m in pattern.finditer(folded)]
    if not spans:
        return text, False
    pieces: list[str] = []
    last_end = 0
    for start, end in spans:
        pieces.append(text[last_end:start])
        pieces.append("[REDACTED]")
        last_end = end
    pieces.append(text[last_end:])
    return "".join(pieces), True


def _run_injection_pass(text: str) -> tuple[str, list[str]]:
    found: list[str] = []
    for label, pattern in _INJECTION_PATTERNS:
        # Re-gera o fold a cada iteracao: o texto muda de tamanho apos cada
        # redacao, entao uma variante "folded" desatualizada desalinharia
        # os offsets do proximo pattern.
        folded = text.translate(_HOMOGLYPH_FOLD_TABLE)
        text, matched = _redact_matches(text, folded, pattern)
        if matched:
            found.append(label)
    return text, found


# --- Deteccao de PII brasileira -----------------------------------------

def _luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _cpf_valid(digits: str) -> bool:
    if len(digits) != 11 or digits == digits[0] * 11:
        return False

    def _dv(prefix: str, start: int) -> int:
        total = sum(int(d) * w for d, w in zip(prefix, range(start, 1, -1)))
        r = (total * 10) % 11
        return 0 if r == 10 else r

    d1 = _dv(digits[:9], 10)
    d2 = _dv(digits[:9] + str(d1), 11)
    return digits[-2:] == f"{d1}{d2}"


def _cnpj_valid(digits: str) -> bool:
    if len(digits) != 14 or digits == digits[0] * 14:
        return False
    w1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    w2 = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]

    def _dv(prefix: str, weights: list[int]) -> int:
        total = sum(int(d) * w for d, w in zip(prefix, weights))
        r = total % 11
        return 0 if r < 2 else 11 - r

    d1 = _dv(digits[:12], w1)
    d2 = _dv(digits[:12] + str(d1), w2)
    return digits[-2:] == f"{d1}{d2}"


# Ordem de aplicacao: cartao -> CNPJ -> CPF -> telefone -> e-mail. Cada
# passo roda sobre a SAIDA do anterior, entao um span ja redigido nunca e
# reexaminado por um padrao mais frouxo depois (evita overlap/double
# redaction sem logica extra de "spans consumidos").
#   - cartao primeiro: e a sequencia de digitos mais longa (13-19); rodar
#     depois de CNPJ/CPF arriscaria casar um pedaco de 11/14 digitos
#     sobrevivente de um cartao ja fragmentado.
#   - CNPJ antes de CPF: CNPJ tem 14 digitos, CPF 11 -- redigir CNPJ
#     primeiro evita que um fragmento de 11 digitos de um CNPJ ja casado
#     sobre e bata com o padrao de CPF depois.
#   - telefone antes de e-mail, com lookahead negativo `(?!@)`: sem isso,
#     o padrao de telefone poderia casar a parte numerica local de um
#     endereco de e-mail antes do proprio e-mail ser processado.
_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_CNPJ_RE = re.compile(r"(?<!\d)(?:\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2})(?!\d)")
_CPF_RE = re.compile(r"(?<!\d)(?:\d{3}\.?\d{3}\.?\d{3}-?\d{2})(?!\d)")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?55\s?)?\(?\d{2}\)?[\s.-]?9?\d{4}[\s.-]?\d{4}(?!\d)(?!@)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _redact_with_checksum(
    text: str,
    pattern: re.Pattern[str],
    validator,
    label: str,
    counts: dict[str, int],
) -> str:
    def _sub(m: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", m.group())
        if validator(digits):
            counts[label] = counts.get(label, 0) + 1
            return f"[PII:{label}]"
        return m.group()

    return pattern.sub(_sub, text)


def _redact_plain(text: str, pattern: re.Pattern[str], label: str, counts: dict[str, int]) -> str:
    def _sub(m: re.Match[str]) -> str:
        counts[label] = counts.get(label, 0) + 1
        return f"[PII:{label}]"

    return pattern.sub(_sub, text)


def _run_pii_pass(text: str) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    text = _redact_with_checksum(text, _CARD_RE, _luhn_valid, "CARD", counts)
    text = _redact_with_checksum(text, _CNPJ_RE, _cnpj_valid, "CNPJ", counts)
    text = _redact_with_checksum(text, _CPF_RE, _cpf_valid, "CPF", counts)
    text = _redact_plain(text, _PHONE_RE, "PHONE", counts)
    text = _redact_plain(text, _EMAIL_RE, "EMAIL", counts)
    return text, counts


# --- API publica -----------------------------------------------------------

def sanitize(text: str) -> SanitizationResult:
    """Limpa `text` (conteudo raspado, adversarial por definicao):
    normaliza Unicode, remove canais invisiveis, redige tentativas de
    prompt injection e redige PII brasileira. Nao gera nonce nem faz
    isolamento estrutural -- ver `wrap_untrusted_content`."""
    normalized = unicodedata.normalize("NFKC", text)
    stripped = _strip_invisible_and_private(normalized)
    collapsed = _collapse_whitespace(stripped)
    after_injection, injection_labels = _run_injection_pass(collapsed)
    after_pii, pii_counts = _run_pii_pass(after_injection)
    return SanitizationResult(
        clean_text=after_pii,
        injection_patterns_found=injection_labels,
        pii_redacted=pii_counts,
        delimiter_escape_attempted=False,
    )


def wrap_untrusted_content(sanitized: SanitizationResult, *, nonce: str | None = None) -> IsolatedPrompt:
    """Gera um nonce por requisicao (`secrets.token_hex`, a menos que
    injetado explicitamente -- so para teste deterministico do escape,
    nunca em producao) e embrulha `sanitized.clean_text` em delimitadores
    que o contem. Se o nonce ja aparecer no proprio texto sanitizado, isso
    e virtualmente impossivel por acaso (128 bits de entropia) e e tratado
    como tentativa de escape do delimitador -- `delimiter_escape_attempted`
    sobe para True na copia de `SanitizationResult` devolvida."""
    if nonce is None:
        nonce = secrets.token_hex(16)

    escape_attempt = sanitized.delimiter_escape_attempted or (nonce in sanitized.clean_text)
    final_sanitized = sanitized.model_copy(update={"delimiter_escape_attempted": escape_attempt})

    wrapped_content = (
        f'<sentinel_untrusted_data nonce="{nonce}">\n'
        f"{sanitized.clean_text}\n"
        f'</sentinel_untrusted_data nonce="{nonce}">'
    )

    return IsolatedPrompt(nonce=nonce, wrapped_content=wrapped_content, sanitized=final_sanitized)
