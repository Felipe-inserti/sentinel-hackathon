# Diagnóstico: imagem no Gemini, coleta de evidência, detecção de formulário de credencial

Encomendado enquanto `obs-2026-08-27` estava pausado (27/08/2026), pra
decidir se vale corrigir a lógica de classificação antes do próximo run.
**Nada foi implementado** — só diagnóstico, confirmado por execução onde
possível, marcado NÃO VERIFICADO onde não dava para confirmar sem
inventar dado.

## a) O Gemini recebe imagem, ou só texto?

**Só texto — confirmado por execução, não por leitura de código.**

Interceptei `LLMClient._call_with_transient_retry` (o ponto exato onde o
SDK do `google-genai` seria chamado de verdade) e rodei
`orchestrator.classify_domain_with_gemini("example.com", "loggi", [])`
de ponta a ponta — scrape real, sanitização real, montagem real do
prompt — parando um instante antes da chamada de rede sair (nenhum custo
de LLM gerado). Capturei o `contents` exato que seria enviado:

```
contents_type: str
contents_len: 277
contents_repr_head: '<sentinel_untrusted_data nonce="8c3b2...">\nExample Domain Example Domain This domain is for use in documentation examples...</sentinel_untrusted_data ...>'
contents_is_bytes_anywhere: False
```

`contents` é uma **string pura** — o SDK do `google-genai` interpreta uma
`str` como uma única Part de texto. Não existe nenhum `Part`/`Blob` de
imagem em lugar nenhum da chamada.

Isso também é estrutural, não só um teste isolado: `classify_domain_with_gemini`
(`plane2_agents/orchestrator.py`) roda **antes** de qualquer screenshot
existir — o `evidence_agent.py` (que captura a imagem) só é acionado
depois, via `investigation-completed`, e só para domínios já classificados
`MALICIOUS`. Cronologicamente é impossível a imagem entrar nesta chamada:
ela não existe ainda quando o Gemini é chamado. `evidence_agent.py` não
importa `llm_client`/`genai`/nenhum cliente de LLM — grep confirma zero
ocorrências.

## b) O que o `evidence_agent.py` coleta — o que entra no prompt vs. o que só é armazenado

**Nada do que segue entra em prompt nenhum.** Tudo é coletado DEPOIS da
classificação, só para o dossiê humano (Firestore + GCS + dashboard).

| Campo (`EvidenceBundle`) | O que é | Entra no prompt? | Onde fica |
|---|---|---|---|
| `screenshot` | PNG full-page via Playwright | **Não** | GCS (`{bucket}/{domain}/{ts}/screenshot.png`), servido pelo dashboard via proxy autenticado |
| `html_snapshot` | HTML sanitizado da página | **Não** | GCS, download via dashboard (`Content-Disposition: attachment`) |
| `http_response` | status code, headers, redirect chain, URL final | Não | Firestore, exibido no painel "HTTP" |
| `dns_records` | A, AAAA, NS, MX, TXT | Não | Firestore, painel "DNS" |
| `hosting` | IP, ASN, org do ASN | Não | Firestore, painel "Hospedagem" |
| `tls_certificate` | issuer, subject, validade, SANs | Não | Firestore, painel "Certificado TLS" |
| `rdap` | registrar, data de criação, idade do domínio, contatos de abuse | Não | Firestore, painel "RDAP" — idade do domínio é o único campo com badge de destaque na UI |
| `infrastructure_fingerprint` | hash do template HTML, IP, ASN, registrar, emissor do cert, hash combinado | Não | Firestore — usado pro MVP de campanhas (`dashboard/.../campaigns`), não pelo LLM |
| `pii_redacted` | contagem de PII redigida (não o valor) | Não | Firestore |
| `form_fields_detected` | ver item (c) abaixo — NÃO é detecção de credencial | Não | Firestore, controla blur do screenshot na UI |
| `collection_errors` | qual etapa falhou e por quê | Não | Firestore, mostrado como "bundle parcial" |
| `manifest_root_hash` | sha256 de todo o bundle (chain of custody) | Não | Firestore |

O que **entra no prompt** é uma coisa completamente separada, coletada
por `orchestrator.scrape_website()` (`requests` + BeautifulSoup, texto
visível, truncado em `MAX_SCRAPED_CHARS=6000`) — texto simples, sem HTML
bruto, sem imagem, sanitizado (`sanitizer.py`) antes de virar `contents`.
Ou seja: **existem dois scrapes independentes do mesmo domínio** — um
barato (texto, alimenta o Gemini, antes da classificação) e um caro
(Playwright completo, só armazenamento, depois da classificação, só se
`MALICIOUS`).

## c) Detecção de formulário de captura de credencial — não existe hoje

O que existe (`_detect_filled_form_fields`, `evidence_agent.py`) é **outra
coisa**: verifica se algum `input`/`textarea`/`select` já chegou com
**valor preenchido** no momento do screenshot (ex: autofill do
navegador/sessão residual) — usado só para decidir se o screenshot deve
nascer borrado por padrão na UI (pode conter dado de vítima já digitado).
Não olha `type="password"`, não olha pra onde o form envia (`action`),
não é sinal de phishing nenhum — é proteção de PII no dossiê.

**Não existe, hoje, nenhum código que:**
- detecta `<input type="password">` na página;
- lê o `action` do `<form>` e compara o domínio de destino contra o
  domínio visitado (o sinal mais forte: página em `x.com` que posta
  credencial pra `y.ru` é phishing quase certo, determinístico, sem
  LLM nenhum).

### Tamanho de implementar

Pequeno e barato — reaproveita o `page` do Playwright que
`_capture_screenshot_and_form_signal` já tem aberto e carregado
(`wait_until="domcontentloaded"`) pro screenshot; **zero custo marginal
de rede/browser**, só mais duas consultas DOM na mesma página já aberta:

1. **`evidence_agent.py`**: novo `CredentialCaptureSignal` (Pydantic,
   mesmo padrão de `FormFieldSignal`) com algo como `has_password_field:
   bool`, `form_action_url: str | None`, `form_action_cross_origin:
   bool`. Nova função `_detect_credential_capture_form(page, domain)`
   (~20-25 linhas: `page.locator('input[type="password"]').count()` +
   `page.locator('form').get_attribute('action')` + comparação de
   origem via `urllib.parse`). Chamada ao lado de
   `_detect_filled_form_fields` em `_capture_screenshot_and_form_signal`.
   Novo campo em `EvidenceBundle`. ~40-50 linhas no arquivo todo.
2. **Dashboard**: novo campo em `dashboard/src/lib/types.ts`
   (`EvidenceBundle`), badge/linha nova em `EvidencePanel.tsx` —
   provavelmente o destaque mais forte do painel, já que é o sinal
   determinístico mais direto de intenção maliciosa que o sistema tem.
   ~15-20 linhas.
3. **Teste**: `tests/test_evidence_agent.py` — fixture HTML local com
   campo de senha + form cross-origin (Playwright consegue servir HTML
   inline via `page.set_content`, não precisa de site real), mais um
   caso negativo (form same-origin ou sem senha). ~25-35 linhas.

Total: ~1-2h de implementação + teste, um arquivo de produção (Python) +
dois de dashboard (tipo + UI) + um de teste. Determinístico, custo zero
de LLM, roda na mesma passada do Playwright que já existe — não é uma
etapa de coleta nova, é enriquecer uma que já roda.

## d) Custo por chamada — com imagem vs. sem

**Sem imagem (real, medido no run `obs-2026-08-27`, texto puro)**:
$3,3322 / 4.664 chamadas reais ao Gemini = **$0,000715/chamada**, usando
os preços configurados (`config.py`): $0,75/milhão tokens de entrada,
$3,75/milhão de saída — mesma fórmula de `telemetry.estimate_cost_usd`,
sem chamar nenhuma API de billing.

**Com imagem: NÃO VERIFICADO, de propósito.** `config.py` não tem
nenhuma constante de preço de token de imagem — o pipeline nunca enviou
imagem, então nunca precisou de uma. Não vou inventar um número aqui: o
próprio `CLAUDE.md` deste projeto é explícito sobre não chutar
especificação de modelo ("consultar documentação oficial em vez de
assumir"), e a tokenização de imagem varia por modelo/resolução/
dimensão da imagem no Vertex AI — precisa ser confirmado na documentação
oficial do `gemini-3.5-flash-lite` (ou o que estiver ativo em
`GEMINI_MODEL_ID` no momento da decisão) antes de estimar, não chutado a
partir de um modelo antigo/genérico. Fica como o primeiro passo se a
decisão for seguir por esse caminho.
