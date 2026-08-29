# Plano — camada de sinais determinísticos antes do Gemini (NÃO implementado)

Encomendado depois do achado de `obs-2026-08-27`: 1.105 MALICIOUS, 0%
phishing genuíno contra nubank/ifood/loggi numa amostra de 30 auditada
manualmente. Este documento é só plano — nenhum código de produção foi
escrito. A pergunta #5 (redução de volume) foi respondida rodando os
sinais de verdade contra dado já coletado, custo zero de LLM.

## 1. HTTP simples vs. Playwright

| # | Sinal | Precisa de Playwright? | Por quê |
|---|---|---|---|
| 2 | Assets do domínio oficial | **Não** | `requests` + BeautifulSoup no HTML estático já é suficiente para achar `<script src>`/`<img src>`/`<link href>` apontando pro domínio oficial — o `orchestrator.scrape_website()` já faz o fetch, só descarta essa informação hoje |
| 3 | Title/metadados | **Não** | Mesmo fetch estático — `<title>`/`<meta>` estão no HTML antes de qualquer JS rodar |
| 4 | Form de credencial + destino | **Quase sempre não** | Forms estáticos (a maioria de kit de phishing barato) aparecem no HTML puro. Exceção: SPA que renderiza o form via JS — nesses casos o fetch estático não vê nada; cai pra `None`/sem sinal, não pra falso negativo silencioso (ver seção 2) |
| 6 | Idade do domínio (RDAP) | **Não** | Nem é fetch pro domínio suspeito — é consulta RDAP, protocolo separado. **Já existe implementado** em `evidence_agent._collect_rdap_domain`, reusável direto |
| 1 | Favicon (hash de bytes) | **Não, na maioria** | Fetch de um arquivo binário (`<link rel="icon">` ou `/favicon.ico`) é HTTP simples. Só quebra se o favicon for setado via JS (raro) |
| 5 | Logo via perceptual hash | **Depende — provavelmente sim** | Se o logo for um `<img>` estático com URL própria, dá pra baixar só aquele arquivo sem browser. Mas confirmar QUE é o logo (não um ícone genérico) e lidar com logo em CSS `background-image`/canvas/SVG-inline exige renderizar — na prática, sem Playwright a taxa de acerto cai muito |

**Resultado**: 5 de 6 sinais (2, 3, 4, 6, e a maior parte do 1) rodam com HTTP simples — maximiza exatamente o que foi pedido. Só o 5 depende de verdade de Playwright, confirmando a suspeita do próprio pedido.

## 2. Onde entra no pipeline

Novo módulo `plane2_agents/deterministic_signals.py`, chamado dentro de
`classify_domain_with_gemini()` (orchestrator.py), **depois** do
`scrape_website()` atual e **antes** da chamada ao Gemini. Aditivo:

- **Não mexe em `scrape_website()`** — a assinatura (`url: str) -> str`)
  é usada com `monkeypatch.setattr(orch, "scrape_website", lambda url:
  "...")` em pelo menos 7 lugares em 4 arquivos de teste
  (`test_orchestrator_cost_guard.py`, `test_orchestrator_brand_memory.py`,
  `test_replay_investigation.py`, e citada em
  `test_injection_cannot_redirect.py`). Mudar o retorno pra uma tupla ou
  dataclass quebra todos esses mocks. A nova função faz **seu próprio
  fetch** (custo: 1 request HTTP extra por domínio, aceitável — muito mais
  barato que uma chamada Gemini). Otimizar pra um fetch único
  compartilhado fica como trabalho futuro explícito, não deste sprint.
- Novo Pydantic model `DeterministicSignals` (mesmo módulo), devolvido
  por `collect_deterministic_signals(url: str, domain: str, brand: str)
  -> DeterministicSignals`. Nenhum contrato existente muda —
  `AnalysisResult` (saída do Gemini) continua igual, só o INPUT fica mais
  rico.
- `_save_investigation` grava `deterministic_signals` como um campo novo
  opcional no dossiê (mesmo padrão de `brand_agent_id`/`evidence_agent_id`
  — aditivo, dashboard ignora até `types.ts` ser atualizado).

### O risco que mais importa: falso negativo

Sua própria regra: "falso negativo é o pior erro". O desenho do funil
("3.186 → sinais → poucos com evidência real → Gemini") implica um
FILTRO de verdade, que reduz volume. Isso tensiona direto com "nunca
perder um malicioso real" — é o mesmo tipo de decisão que já existe hoje
pra Gemma triage, e a solução já está no código: **fail-open**.

Recomendo NÃO deixar esta camada decidir sozinha. Duas opções, preciso
que você escolha:

- **Opção A (mais segura, sem redução de volume ainda)**: sinais só
  ENRIQUECEM o prompt, nunca filtram. Todo domínio que sobrevive ao
  prefiltro continua indo pro Gemini — só que agora com um dossiê
  estruturado em vez de 6000 chars de texto genérico. A redução de
  volume desta rodada é zero; o ganho é qualidade de veredito. Mais
  seguro, mais fácil de validar, não risca a garantia de recall.
- **Opção B (reduz volume, mais risco)**: só descarta antes do Gemini
  quando **dois sinais independentes concordam** — Gemma triage já disse
  DISCARD **E** nenhum sinal determinístico disparou. Mesma filosofia de
  fail-open da Gemma (qualquer incerteza/falha em QUALQUER uma das duas
  camadas força escalada) — nunca uma decisão de um só lugar. Reduz
  volume de verdade, mas precisa da validação contra PhishTank (seção
  "Validação obrigatória" do seu pedido) rodando ANTES de considerar
  pronto, não depois.

Os números da seção 5 abaixo servem pras duas opções — o que muda é só
quando eles entram em vigor (informativo vs. filtro).

## 3. Como entra no prompt — estruturado, nunca texto solto

`DeterministicSignals.model_dump_json()` (ou `sanitize()` campo a campo
primeiro — ver abaixo) vira um bloco JSON **dentro do MESMO delimitador
não confiável** que já embrulha o texto raspado — mesmo nonce, mesma
detecção de escape de `sanitizer.wrap_untrusted_content`, exatamente o
padrão que `brand_memory` (Sprint 7B) já usa pros exemplos few-shot.
**Nunca um segundo canal** — seria reabrir a regra de segurança #1 do
projeto ("conteúdo raspado é adversarial por definição").

Cuidado extra: campos do sinal que carregam STRING derivada da página
(nome do domínio de asset, texto do title) continuam sendo dado
adversarial, mesmo estruturado — passam por `sanitizer.sanitize()` antes
de entrar no JSON, igual ao texto solto. Campos booleanos/numéricos
(`has_password_field: bool`, `domain_age_hours: float`) não precisam de
sanitização (não são texto livre), mas o JSON inteiro ainda entra dentro
do bloco delimitado, por disciplina — nunca dividir o que é "seguro" do
que não é dentro do mesmo prompt.

O `system_prompt` (`llm_client.generate`) ganha uma seção nova instruindo
como pesar os sinais: reuso de asset oficial ou nome da marca em
title/meta é evidência forte de personificação; ausência de qualquer
sinal estrutural, combinado com colisão de string genérica, deve pesar
pra baixa confiança/SAFE — mas a decisão final continua do modelo, nunca
hardcoded.

## 4. Estimativa por sinal

| Sinal | Linhas (produção) | Horas | Teste |
|---|---|---|---|
| 2. Assets oficiais | ~25 (parse HTML, comparar contra `prefilter.TRUSTED_DOMAINS`, reusa a constante que já existe) | 0,5h | 1 positivo + 1 negativo, ~15 linhas |
| 3. Title/metadados | ~20 (extrair `<title>`/`<meta>`, comparar contra nome da marca) | 0,5h | 2 casos, ~15 linhas |
| 4. Form de credencial | ~35 (`input[type=password]`, `<form action>`, comparação de origem via `urllib.parse`) | 1h | 3 casos (same-origin, cross-origin, sem senha), ~30 linhas |
| 6. Idade do domínio | ~10 (chamar `evidence_agent._collect_rdap_domain` já existente, sem reimplementar) | 0,25h | reusa fixture RDAP já existente em `test_evidence_agent.py` |
| 1. Favicon hash | ~40 (fetch + sha256, manter um cache pequeno de hash oficial por marca, atualizado manualmente ou por script separado) | 1,5h | 2 casos (match, no-match) + fixture de hash oficial, ~25 linhas |
| 5. Logo pHash | ~80-100 (extração de `<img>` candidato, download, `imagehash`/`Pillow` — dependência nova — comparação contra 3 logos oficiais cacheados) | 3-4h | mais frágil de testar (imagem real), ~40 linhas |
| **Orquestração** (`collect_deterministic_signals`, model Pydantic, wiring no orchestrator, campo novo no dossiê) | ~50 | 1h | integração, ~20 linhas |
| **Prompt** (system_instruction novo + wrapping no bloco delimitado) | ~15 | 0,5h | reusa testes de `test_injection_cannot_redirect.py` como referência |

**Total (sinais 1,2,3,4,6 + orquestração + prompt, sem o 5)**: ~215
linhas de produção, ~6-6,5h, ~105 linhas de teste.
**Com o 5**: +80-100 linhas, +3-4h, +40 linhas de teste — quase dobra o
esforço pro sinal mais fraco no experimento offline (não testável sem
dado novo, ver seção 5).

## 5. Redução de volume — medido, não estimado

Rodei os sinais 2, 3, 4 e 6 **de verdade** contra os 191 dos 1.105
MALICIOUS de `obs-2026-08-27` que já têm `html_snapshot`/RDAP
armazenados no GCS/Firestore (zero chamada nova ao Gemini, zero custo de
token). Sinal 1 (favicon) e 5 (logo) **não são testáveis offline** com o
dado já coletado — `evidence_agent.py` não armazena os bytes do favicon
nem um crop de logo, só o HTML e o screenshot full-page; medir esses
dois exigiria fetch novo, fora do escopo desta rodada de diagnóstico.

| Sinal | Hits / 191 |
|---|---|
| 2. Assets do domínio oficial | **0** |
| 3. Nome da marca em title/meta | **0** |
| 4. Campo de senha presente | 19 |
| 4b. Senha **+ form cross-origin** | 3 |
| 6. Domínio jovem (<7 dias), das 28 com RDAP coletado | 1 |

**"Qualquer sinal dispara" (contagem ingênua)**: 20/191 = 10,5% sobreviveriam.

**Mas o sinal que domina essa contagem é ruído**: dos 19 hits de "campo
de senha", a esmagadora maioria é `webmail.*`/`cpanel.*` — login padrão
de painel de hospedagem (Roundcube/cPanel), presente em qualquer conta
de hosting, sem NENHUMA relação com phishing de marca. Exatamente o
alerta que você mesmo fez no pedido ("campo de senha sozinho não vale
nada"): confirmado com dado real.

**Usando só os sinais de personificação de marca de verdade (2 OU 3 OU
4b — reuso de asset, menção da marca, ou form cross-origin)**: survival
cai pra **3/191 = 1,6%** — e nem esses 3 miram nubank/ifood/loggi
(`reffonly.me/game`, `greniersolutions.com/.../contact/submit` — alvos
não relacionados, mesma conclusão da segmentação manual de 30).

**Isto corrobora, por um caminho totalmente independente, o achado da
segmentação manual (0/30 phishing genuíno)**: dois métodos diferentes
(auditoria humana de reasoning vs. sinal estrutural automatizado em HTML
real) chegam à mesma conclusão — este lote de MALICIOUS quase não tem
personificação de marca de verdade.

**O número que falta, e que decide se o plano se sustenta**: recall
contra phishing REAL (corpus PhishTank, seção "Validação obrigatória" do
seu pedido) não foi medido ainda — não tenho amostra de phishing
confirmado com `html_snapshot` já coletado. Sem isso, não dá pra saber
se os mesmos sinais que corretamente rejeitam os 191 aqui também
corretamente ACEITAM phishing de verdade. Essa validação é obrigatória
antes de considerar a Opção B (filtro) da seção 2 seguro — a Opção A
(enriquecimento, sem filtro) não depende dela pra ser segura, só pra ser
mais precisa.

## 6. Se só coubessem 4 horas

Cortaria o **sinal 5 (logo pHash)** inteiro — é o mais caro (3-4h
sozinho, quase metade do orçamento total), depende de Playwright (quebra
"maximizar HTTP simples"), introduz dependência nova (`imagehash`/
`Pillow`), e no experimento offline nem dava pra medir contribuição real.
Cortaria também o **sinal 1 (favicon)** por segundo — não é caro em
horas (1,5h) mas exige manter um cache de hash oficial atualizado
manualmente (ponto de manutenção contínuo, não é "escreve uma vez e
esquece" como os outros), e não pude validar contribuição real no
experimento offline pela mesma limitação de dado.

Ficam **2, 3, 4, 6 + orquestração + prompt**: ~6-6,5h, dentro do
orçamento, e são exatamente os 4 sinais que o experimento offline
conseguiu medir de verdade — não sobra nenhum sinal "no escuro" na
primeira entrega.

## Validação obrigatória (depois de implementar — não fiz ainda)

- Reprocessar os 1.105 MALICIOUS de `obs-2026-08-27` (`html_snapshot`
  já no GCS, zero custo) com os sinais reais implementados — deve cair
  drasticamente, na faixa do medido acima (~0-2% pros sinais fortes).
- Rodar contra o corpus PhishTank confirmado (mesmo corpus de
  `FINDINGS.md` item 10 — nubank/ifood/loggi, 8 casos reais confirmados)
  — **recall não pode cair**. Ainda não tenho `html_snapshot` desse
  corpus coletado; precisaria rodar `evidence_agent.collect_evidence`
  contra esses 8 domínios primeiro (zero custo de Gemini, é só
  Playwright) antes de poder medir.

## Resultado da validação de recall — MEDIDO, 2026-08-28: recall baixo, Opção B REJEITADA

Rodado antes de qualquer implementação, como pedido explicitamente.
Corpus: feed completo do PhishTank (`data.phishtank.com/data/online-valid.json`,
73.717 entradas, sem precisar de API key), filtrado para
`verified=yes` + `online=yes` e para menção de marca bancária/fintech BR
(não só nubank/loggi/ifood — também itaú, bradesco, caixa, santander,
picpay, banco inter, btg pactual, por pedido explícito). Dedup por host.
Bradesco (298 hosts únicos brutos) capado em 40 amostras aleatórias pra
não dominar o corpus com um kit só; as demais marcas entraram inteiras.
**Corpus final: 106 casos confirmados.**

Dois bugs de colisão de string encontrados e corrigidos NA CONSTRUÇÃO do
corpus, mesma classe do achado "loggin≠Loggi" de `FINDINGS.md` item 10:
"itaú" apenas removendo caracteres não-alfanuméricos vira "ita" (não
"itau" — a letra acentuada inteira é descartada, não substituída pela
base), colidindo com qualquer URL contendo "digital"; e concatenar
labels de host sem o ponto entre eles cria colisão de fronteira
(`...maxklog` + `gitbook.io` → contém "loggi" por acidente). Corrigido
com normalização NFKD (preserva a base da letra) e matching por label/
segmento, nunca no host+path concatenado inteiro.

**Fetch estático (`requests`, timeout 8s, SEM Playwright — os 4 sinais
medidos não precisam de browser, ver seção 1 acima; validar com o mesmo
mecanismo que a produção usaria)**:

| | Contagem | % |
|---|---|---|
| Corpus total | 106 | 100% |
| Site respondeu (fetch OK) | 34 | 32,1% |
| Fora do ar/erro (ConnectionError 30, HTTP 451/404/403 36, timeout/outros 6) | 72 | 67,9% |

**Por sinal, sobre os 34 que responderam:**

| Sinal | Hits / 34 |
|---|---|
| 2. Assets do domínio oficial | **0** |
| 3. Nome da marca em title/meta | 7 |
| 4. Campo de senha presente | 5 |
| 4b. Senha + form cross-origin | 1 |
| 6. Domínio jovem (RDAP < 7 dias) | 0 (de 102 com dado RDAP) |

**"2 OU 3 OU 4b" (personificação real), sobre os 34 que responderam:
8/34 = 23,5%.** Sobre o corpus inteiro de 106 (denominador pessimista,
tratando fora-do-ar como não-detectado): 8/106 = 7,5%.

De revisão manual desses 8: pelo menos 1 é alvo errado
(`bancointernacion.webcindario.com` mira "Banco Internacional del
Ecuador", não Banco Inter BR — mesma classe de colisão de substring,
"bancointernacion" contém "bancointer") e 1 é ambíguo
(`itau.com.py` pode ser o domínio legítimo do Itaú Paraguai, não um
clone). Recall "limpo" fica entre 6/34 (17,6%) e 8/34 (23,5%).

**Muito abaixo do limiar de 70% combinado como critério de decisão.
Opção B (filtro) está REJEITADA por este resultado — não implementada.**

### Causa raiz, não só o número

- **Kits SPA renderizados por JS dominam o corpus que responde.**
  Inspecionando os "falsos negativos" com fetch OK
  (`itau-landing.vercel.app`, `leia-santander.vercel.app`,
  `particulares-netbancosantander.{web,firebaseapp}.app` — Next.js/Nuxt
  hospedados em Vercel/Firebase): o HTML estático devolvido é só o shell
  do framework. Título genérico ou decoy (`"Ingresando..."`,
  `"LikeU"`, `"Netbanco Particulares"` sem marca), nenhum asset da marca
  oficial, nenhum `<form>`/`<input type=password>` — tudo isso é
  injetado por JavaScript client-side DEPOIS do carregamento. Grep
  direto por `"itau.com.br"`/`"santander.com.br"` no HTML bruto desses
  casos confirma: a string simplesmente não está lá, não é bug do
  detector. Isto é exatamente a exceção que a seção 1 deste documento já
  cogitava ("SPA que renderiza o form via JS... cai pra None/sem sinal")
  — mas a validação mostra que não é exceção rara, é o padrão dominante
  entre os kits modernos que ainda estão no ar.
- **Sinal 6 (idade do domínio) ficou estruturalmente cego neste corpus,
  não por threshold errado.** RDAP resolve pelo domínio APEX
  (`vercel.app`, `webcindario.com`, `duckdns.org`, `firebaseapp.com`) —
  a data de registro que ele devolve é de quando a PLATAFORMA de
  hosting gratuito foi registrada (anos atrás), não de quando o
  subdomínio do atacante foi criado. Para o padrão de hosting que domina
  este corpus (hosting/subdomínio gratuito), sinal 6 não tem como
  funcionar sem uma fonte de "idade do subdomínio" que RDAP não
  fornece — não é uma questão de recalibrar o limiar de 7 dias.
- **68% do corpus confirmado já está fora do ar.** Efêmero por natureza
  do próprio ataque — não é falha de metodologia de validação, mas
  reduz ainda mais o corpus onde os sinais têm chance de disparar.

### Amostra pequena — mesma honestidade de `FINDINGS.md` item 10

34 casos com fetch OK (8 com sinal) é maior que os 8 do corpus de
nubank/ifood/loggi de `FINDINGS.md`, mas ainda não sustenta um intervalo
de confiança apertado. O número reportado (17,6%–23,5%) é uma contagem
bruta, não uma taxa populacional precisa — mas está longe o suficiente
do limiar de 70% para não depender dessa precisão: mesmo no limite
otimista (8/34), a distância pro limiar é grande demais pra ser efeito
de amostra pequena.

### O que isso significa pra Opção A (enriquecimento, sem filtro)

Esta validação **não invalida a Opção A**. Os sinais que dispararam (title/
meta, form cross-origin) continuam evidência estrutural real quando
disparam — só não disparam com frequência suficiente pra servir de
FILTRO com o recall que a Opção B exigiria. Enriquecer o prompt do
Gemini com esse dossiê estruturado (quando disponível) continua seguro:
não reduz volume, não arrisca recall, e melhora a qualidade do veredito
nos casos em que o fetch estático realmente vê algo.

### Reprodutibilidade

Script de construção do corpus e de medição dos sinais rodados fora do
repositório (`/tmp/.../scratchpad/build_corpus.py`,
`run_signals.py` — não são código de produção, não commitados). Dados
brutos do PhishTank (`online-valid.json`) e resultados
(`corpus.json`, `signal_results.json`) preservados na mesma pasta de
scratchpad da sessão que gerou este resultado, não versionados no repo.
