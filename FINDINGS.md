# FINDINGS — Camada de Triagem Gemma (Sprint 3)

Todos os números abaixo foram medidos rodando `eval_triage.py` contra um
Gemma 3 270M real (Ollama 0.32.15, `gemma3:270m`, CPU/GPU conforme
disponível no host), não estimados. Reproduza com:

```
ollama serve &
ollama pull gemma3:270m
python eval_triage.py --with-gemini
```

## 1. Escolha de modelo e serving (resumo — detalhes em `gemma_triage.py`)

- **Gemma 3 270M**, confirmado como a menor variante Gemma atual desenhada
  para classificação/roteamento (não geração livre), via anúncio oficial
  do Google Developers Blog.
- **Cloud Run CPU + Ollama**, não Vertex AI Model Garden (cobra por
  node-hora contínua, mesmo ocioso) nem a API gerenciada do Gemini (só
  expõe as variantes grandes do Gemma 4). Cloud Run escala a zero por
  padrão — custo real zero fora da janela de uso.

## 2. Latência medida

| Cenário | Latência |
|---|---|
| Cold start (modelo não carregado) | ~36s (17.5s carregar o modelo + 16.4s prompt eval + 2s geração) |
| Chamada simples, modelo já carregado | ~62ms |
| Lote de 5 domínios (schema JSON completo) | ~650–1000ms total (~150–200ms/domínio) |
| Lote de 17 domínios, chamado em 4 sub-lotes de 5 (padrão de produção) | ~8s total (~470ms/domínio) |

**Implicação prática**: o cold start de ~36s só acontece na primeira
chamada após o container subir (ou após ficar ocioso além do
`OLLAMA_KEEP_ALIVE`, 5min por padrão). Para a janela de demo, o script de
deploy recomenda manter `--min-instances=1` durante a gravação
especificamente para evitar esse cold start aparecer ao vivo.

## 3. Achado crítico: tamanho de lote — o exemplo do requisito (20) é
   otimista demais para este modelo

Testei `_call_ollama_once` diretamente contra domínios sintéticos
idênticos em estrutura, variando o tamanho do lote, 3 tentativas cada:

| Tamanho do lote | Taxa de sucesso (schema completo) |
|---|---|
| 2 itens (heterogêneos, incluindo um domínio muito diferente dos exemplos few-shot) | 5/8 (62%) |
| 5 itens (homogêneos) | 3/3 (100%), reproduzido em execuções separadas |
| 8 itens | 0/3 (0%) — o modelo trunca ou degenera em repetição |
| 10 itens | 0/3 (0%) |
| 17 itens (dataset rotulado inteiro em uma chamada só) | 0/2 tentativas — JSON truncado no meio de uma string, cai no fail-open para o lote inteiro |

**Conclusão**: `gemma_batch_max_size` foi ajustado de 20 (valor de exemplo
do requisito) para **5** em `config.py`, com o motivo documentado no
comentário do campo. Isso não é uma limitação de infraestrutura (CPU
suficiente, sem timeout) — é a capacidade de geração estruturada do
modelo de 270M com este prompt (few-shot + schema JSON aninhado) que
degrada abruptamente acima de ~5-7 itens. Um lote de 20 itens quase
sempre vai cair em fail-open (todo mundo vira INVESTIGATE) em vez de
realmente triar — na prática isso ainda é seguro (fail-open nunca perde
um falso negativo silencioso), mas anula o ganho de custo da camada.

## 4. Métricas no conjunto rotulado (17 casos: 10 maliciosos, 4 legítimos
   com similaridade coincidente, 3 casos-limite)

Rodado com o `gemma_batch_max_size=5` corrigido (4 sub-lotes, replicando
exatamente o comportamento de produção do `ct_listener.py`):

| Métrica | Valor |
|---|---|
| TP / FP / TN / FN | 11 / 4 / 1 / 1 |
| **Precisão** | 73.3% |
| **Recall** | **91.7%** |
| Acurácia | 70.6% |
| **Taxa de falso negativo** | **8.3%** (1 de 12 maliciosos) |
| Custo total (17 domínios) | $0.000033 |

O único falso negativo foi `ifood-parceiros-oficial.com.br` (rótulo:
malicioso — isca de cadastro de parceiro). O modelo devolveu
`DISCARD, risk_score=0.15` com uma `rationale` que é **cópia quase
literal** do texto de um dos exemplos few-shot (`promocoes-ifood-
parceiros.com.br`, rotulado DISCARD no prompt) — evidência de que o
modelo de 270M está ancorando na similaridade textual/estrutural com o
exemplo mais parecido em vez de raciocinar sobre os sinais específicos do
caso (a idade do certificado nos dois casos é bem diferente: 2 anos no
exemplo vs. 30 dias no caso real, e o modelo não usou essa diferença).

**Isso é uma limitação real e esperada de um modelo desse tamanho**, não
um bug de código — está documentada aqui em vez de escondida.

## 5. Comparação com o Gemini (requisito 11)

Não foi possível rodar neste ambiente: as credenciais do Vertex AI
disponíveis não têm a API do Vertex habilitada no projeto de teste usado
(`aiplatform.googleapis.com`, erro `SERVICE_DISABLED` real, capturado ao
vivo). `eval_triage.py --with-gemini` está implementado e funcional (usa
os mesmos sinais estruturados, sem scraping, para comparação justa de
custo/latência por decisão) — precisa rodar contra um projeto GCP real
com Vertex habilitado e `GEMINI_MODEL_ID` configurado. Deixado como
próximo passo explícito, não inventado.

## 6. Recomendações para melhorar o recall (requisito 12: priorizar
   recall sobre precisão)

Com a taxa de falso negativo atual em 8.3%, ações concretas de próximo
passo, em ordem de esforço:

1. **Mais exemplos few-shot, mais diversos** — o caso de falha sugere que
   3 exemplos não bastam para o modelo generalizar além de similaridade
   textual superficial com os próprios exemplos.
2. **Piso de segurança determinístico**: se `verdict == DISCARD` mas
   `risk_score` acima de um limiar configurável (ex: 0.3), forçar
   `INVESTIGATE` mesmo assim — um "cinto de segurança" fora do controle
   do modelo. (Não implementado ainda porque o falso negativo observado
   teve `risk_score=0.15`, abaixo de qualquer limiar razoável — não
   teria pego este caso especificamente, mas ajudaria em casos de
   incerteza genuína do modelo, então ainda vale a pena.)
3. **Lotes menores e mais homogêneos** (já aplicado, item 3) — reduz a
   chance de o modelo "confundir" sinais entre domínios muito diferentes
   dentro do mesmo lote.

## 7. Fail-open (requisito de aceite)

Comprovado por teste automatizado que derruba o serviço de verdade (nível
`httpx`, não só a função `triage_batch` mockada em alto nível) —
`tests/test_ct_listener_triage_integration.py::test_fail_open_when_gemma_service_is_down`
e `tests/test_gemma_triage.py::test_triage_batch_fails_open_when_service_is_down`.
Em ambos, com o Gemma inacessível, **zero domínios são descartados** —
todos viram `INVESTIGATE`.

## 8. Registry (requisito 13)

TODO explícito: publicar `gemma-triage-agent` no Agent Registry quando
existir um mecanismo real de registro no projeto (nenhum cliente/API de
Agent Registry foi integrado neste sprint — não há como verificar o
formato certo sem mais contexto sobre qual Agent Registry o requisito se
refere, e inventar a integração seria o tipo exato de "chute" que este
projeto evita). Ver comentário `TODO(agent-registry)` em `gemma_triage.py`.

## 9. Prova adversarial — injeção não redireciona o takedown (Sprint 6)

Relatório completo em [`docs/adversarial_report.md`](docs/adversarial_report.md),
gerado a partir de `tests/test_injection_cannot_redirect.py` (7 cenários
mockados — sempre rodam em CI — + 3 equivalentes contra o Gemini real via
`pytest -m live_llm`, opt-in manual, não rodados nesta sessão). Prova, em
pior caso (assume que o LLM já "caiu" na injeção), que um payload plantado
no texto sanitizado da página, no título/meta description, no contato de
abuso do RDAP, em português/inglês, ou escondido em caracteres Unicode
invisíveis (Tag Characters) nunca consegue trocar o destinatário de uma
notificação de takedown, adicionar um destinatário extra, escalar para um
canal fora da categoria que o humano aprovou, pular a verificação de
aprovação, ou desligar o `DRY_RUN`. **Resultado: 7/7 cenários mockados
bloqueados.**

Achado real, corrigido no mesmo sprint (não só um teste que passou por
sorte): `resolve_abuse_contacts` (`takedown_agent.py`) confiava
cegamente em qualquer string devolvida pelo RDAP como contato de abuso —
um valor mal-formado tipo `"abuse@legit.com, atacante@evil.com"` seria
usado verbatim, e um remetente real futuro interpretaria a vírgula como
um segundo destinatário. Corrigida com `_is_single_valid_contact`: só um
único endereço/URL bem formado é aceito, de RDAP ou de tabela fixa —
qualquer outra coisa é tratada como não-resolvível, nunca particionada.

## 10. Taxa de escape do prefilter — investigação com dados reais (Sprint de medição de custo)

Disparada por uma medição de 31 min contra o Certificate Transparency real
(`OBSERVATION_RUN_ID=obs-medicao-2026-08-27`): 450.247 avaliações de
domínio, 93,67% descartadas pelo prefiltro — abaixo da tese documentada de
~99%, e acima do limiar de anomalia configurado (5%, disparou `CRITICAL`
corretamente).

### Causa raiz dos 28.515 escapes: ruído estatístico, não falha de detecção

Reanalisando os 28.515 domínios reais que escaparam (`analyze_domain()`,
grátis, local): **99,8% escaparam pela heurística `sliding_window`**
(distância de edição ≤ `max_edit_distance`), não pela similaridade de
token. 85% tinham score exatamente no piso (distância = 2, o máximo então
aceito) contra hostnames de infraestrutura legítima de altíssima entropia
— IDs do WorkDay (`*.prd.workdaysuv*.com`), device IDs do Synology
(`*.myvolumio.org`), hashes do Cloudflare Workers/Pages, hostnames
internos AWS/Azure — e, pior caso encontrado, **spam de loteria chinesa em
`.casa`** batendo em "nubank" por pura coincidência de distância de
edição. A tradução de leetspeak (dígito→letra) piora isso: transforma hex
aleatório em algo "parecido com palavra".

### O bug encontrado: por que o recall não era 100% nem em distância=2

Investigando por que o recall contra um corpus sintético de typosquats
clássicos não batia 100% mesmo na configuração mais permissiva
(`max_edit_distance=2`), encontramos que `analyze_domain()` só gravava a
distância de edição qualificada (`best_distance`) quando ela **também**
superava o melhor score de similaridade de token já visto — mesmo esse
score estando abaixo do limiar de suspeita (0,82). Um match de distância
de edição genuíno era descartado silenciosamente sempre que qualquer outra
coisa não-suspeita tivesse pontuado mais alto antes. Impacto medido: 0% de
recall em transposição adjacente e homoglyph de "loggi" (`lgogi.com`,
`olggi.com`, `logg1.com` etc.) — **independente do valor de
`max_edit_distance` escolhido**. Mesmo padrão de falha do achado de RDAP
(SS9 acima): um "cinto de segurança" com condição de ativação errada é
pior que a ausência dele, porque cria falsa sensação de cobertura. Corrigido
gravando `best_distance` de forma independente do score de token (ver
comentário no código); coberto por
`tests/test_prefilter.py::test_qualified_edit_distance_match_is_not_discarded_by_lower_token_score`.

### Recall medido, com honestidade de amostra

- **Corpus real confirmado** (PhishTank, filtrado manualmente para
  nubank/ifood/loggi entre 18 batidas brutas de string — 10 eram falso
  match do próprio filtro, ex: "loggin"=login em inglês, não Loggi): **7
  de 8 casos confirmados** pegos, em `max_edit_distance=1` OU `2`, antes E
  depois do fix — nenhuma das quatro combinações mudou esse número. Oito
  amostras não sustentam uma taxa percentual confiável; reportamos a
  contagem bruta, não "87,5% de recall".
- **Corpus sintético** (5.413 candidatos: homoglyph, inserção, deleção,
  transposição adjacente, duplo-edit, hífen — 3 marcas): recall subiu de
  95,5% (com o bug) para 99,9% (`max_edit_distance=2`, com o fix) ou 99,6%
  (`max_edit_distance=1`, com o fix). A diferença de 0,3pp entre os dois
  thresholds é toda explicada por transposições adjacentes cuja distância
  de edição *padrão* (não Damerau-Levenshtein) é 2, não 1 — ex:
  `lgogi.com` contra "loggi" (ver teste
  `test_loggi_transposition_requiring_distance_2_is_not_caught_at_default`).

### O caso perdido — limitação estrutural, não corrigível hoje

O único caso do corpus real não capturado (`br-2421-...vercel.app`, alvo
"nubank" confirmado pelo próprio PhishTank) não tem nenhum texto de marca
no domínio — é um hash de deploy Vercel. Nenhuma heurística de
similaridade textual sobre o NOME do domínio consegue capturar isso; exige
sinal fora do domínio (conteúdo da página, infraestrutura compartilhada
com ataques conhecidos). Registrado como limitação estrutural da Camada 1,
não como algo a corrigir nesta sprint.

### Decisão aplicada

`DEFAULT_MAX_EDIT_DISTANCE`: 2 → 1. Medido, não estimado: descarte do
prefiltro passa de 93,67% para 99,50% (redução de 92% no volume que chega
ao Gemma/Gemini) contra os mesmos 450.247 domínios reais, com recall
idêntico nos dois corpus de teste (real: 7/8 nos dois valores; sintético:
diferença de 0,3pp, explicada acima e não relacionada a nenhum ataque
encontrado nos dados reais ou no corpus sintético).

### Limitações registradas, não resolvidas nesta sprint (por decisão explícita)

- **Colisão de dicionário**: "ifood" e "loggi" compartilham substring com
  palavras comuns (`food`, `logi`/`logística`) em português e inglês —
  gera falso positivo (`tarazifoods.com`, `globalogic.com.ph`,
  `andrologie-ochsenfurt.de`) mesmo em `max_edit_distance=0` (substring
  exato). Não é problema de threshold, é escolha de marca curta/genérica;
  precisaria de uma abordagem diferente (ex: exigir limite de token, lista
  de exclusão de falsos positivos conhecidos) — fora do escopo de hoje.
- **Bypass de allowlist via normalização de homoglyph** (achado
  incidental ao verificar o fix acima, NÃO corrigido): `normalize_domain()`
  aplica a mesma tabela de tradução dígito→letra usada para DETECTAR
  homoglyphs também na checagem contra `TRUSTED_DOMAINS` — um domínio como
  `1oggi.com` ou `l0ggi.com` normaliza para a string exata `loggi.com` (o
  domínio legítimo) e é classificado como seguro pela allowlist, em vez de
  suspeito. Ou seja: a mesma tradução que deveria ajudar a PEGAR um
  homoglyph pode, neste caminho específico, fazer um homoglyph passar como
  o domínio confiável. Reportado, sem fix aplicado — decisão explícita de
  não gastar tempo nisso nesta sprint; precisa de decisão de design (checar
  a allowlist contra o domínio bruto antes da normalização, ou tratar
  colisão pós-normalização com um domínio confiável como sinal de alerta
  em vez de safe-pass).

## 11. Consolidação — sprint de troca de fonte CT, paralelização e custo (27/08/2026)

Números espalhados pela sessão, reunidos aqui num lugar só. Tudo medido
por execução real (Argon2026h2 ao vivo, ou run real de 31min contra
Firestore/Pub-Sub/Gemini reais) — nada estimado sem dizer que é estimativa.

### Vazão de ingestão do CT: sequencial → paralelo

| | Vazão medida | Produção real do log (mesma janela) | Gap projetado em 48h |
|---|---|---|---|
| Sequencial (1 faixa, polling RFC 6962 puro) | 48,9 entradas/s | 203,4/s | ~36,5h de atraso ao final |
| Paralelo (múltiplas faixas, concorrência adaptativa) — corrida A | 115,2/s | 166,2/s | ~14,7h |
| Paralelo — corrida B | 112,6/s | 223,6/s | ~23,8h |

A variação entre as corridas paralelas (14,7h vs 23,8h) é real, não erro
de medição — vem de dois fatores observados ao vivo: (1) a produção real
do log Argon2026h2 varia sensivelmente entre janelas de amostragem curtas
(166–224/s medidos em janelas de 90–120s), e (2) o rate limit do log
interrompe a concorrência ocasionalmente (ver abaixo), reduzindo a vazão
efetiva até o recuo se recuperar.

### Rate limit não documentado do Argon2026h2 — descoberto por execução

Nenhuma fonte oficial documenta um rate limit para este log (verificado na
pesquisa que precedeu esta sprint). **Existe, e foi atingido ao vivo, duas
vezes**, em corridas independentes de medição de vazão paralela: 3 eventos
de HTTP 429 reais em cada uma. O controlador de concorrência (`_ConcurrencyController`,
`plane1_ingestion/ct_listener.py`) reduziu a concorrência pela metade a
cada 429 e voltou a subir gradualmente sem travar — comportamento
verificado, não assumido, nas duas corridas (concorrência observada
oscilando 4→2→1→2 numa e 1→1→1→2→3→4→5 na outra). Cobertura de teste:
`tests/test_ct_listener_parallel_ingestion.py`.

### Custo real de Vertex AI (Gemini) por chamada

Medido no run de 31min de 27/08/2026 (`obs-medicao-2026-08-27`, real,
Firestore/Pub-Sub/Gemini reais): **US$0,000885 por chamada** (US$1,8552 /
2.096 chamadas). Nota de honestidade: essa medição rodou com o Gemma
indisponível (fail-open — ver item 7 acima e `README.md`), então o volume
que chegou ao Gemini nessa amostra é piso conservador (pior caso), não o
número esperado de produção com a cascata completa funcionando.

### Taxa de escape real do prefiltro e o alarme de anomalia

Na mesma medição de 31min: 6,33% de escape (28.515 de 450.247), **acima
do limiar de anomalia configurado (5%)** — `observation_run.check_prefilter_escape_anomaly()`
disparou `CRITICAL` corretamente, repetidas vezes, ao longo do run (não só
uma vez no início) — o mecanismo de alarme funcionou exatamente como
projetado, é o que permitiu perceber o problema em horas, não no fim do
run de 48h. Investigação completa da causa raiz, o bug de combinação de
sinais encontrado, e a correção aplicada (93,67%→99,50% de descarte): item
10 acima.

### O padrão que mais custou nesta sprint: suíte verde não prova produção

Achado do sprint anterior (Etapa C), reafirmado aqui porque voltou a
importar: `observation_run.py` tinha cobertura de teste completa e 264
testes verdes, mas ficou **ausente da imagem Docker** — o `Dockerfile`
usa lista explícita de arquivos top-level (não `COPY . .`), e o módulo
novo nunca foi adicionado a essa lista. Os workers em produção crasharam
importando um módulo "testado". Nesta sprint, todo módulo novo
(`plane1_ingestion/ct_rfc6962.py`) teve sua presença na imagem **confirmada
por execução** (simulação das linhas `COPY` reais + `.dockerignore`,
não leitura do `Dockerfile`) antes de ser considerado pronto — ver
`README.md` para o resumo e a lista completa de "o que acontece quando
quebra".

## 12. Dashboard ponta a ponta contra dados reais (27/08/2026)

Verificação por execução do dashboard já deployado (`sentinel-dashboard`,
redeploy de 25/08 — antes das mudanças de CT/prefilter/schema desta
sprint) contra os 273 dossiês `PENDING_HUMAN_REVIEW` reais gerados pela
medição de 31min. IAM confirmado por execução, não por leitura do
`infra/`: `dashboard-sa` tem `roles/datastore.user` (Firestore),
`roles/storage.objectViewer` no bucket de evidência, e é a ÚNICA
identidade com `roles/pubsub.publisher` em `takedown-approved` — bate com
o que `CLAUDE.md`/`infra/README.md` descrevem.

### Achado real, corrigido: `investigated_at` virava "Invalid Date" na Timeline

`plane2_agents/orchestrator.py::investigate_domain` grava `investigated_at`
como `datetime.now(timezone.utc)` cru (dict literal, não passa por
`model_dump(mode="json")` do Pydantic como `evidence_agent.py` faz para
`collected_at`). O Admin SDK do Node devolve isso como um `Timestamp` do
Firestore sem `toJSON()` — `JSON.stringify` direto nele produz
`{"_seconds":...,"_nanoseconds":...}`, e todo `new Date(...)` do lado
cliente (`Timeline.tsx`, `CloudTraceLink.tsx`) virava literalmente
**"Invalid Date"** na tela. Sistêmico (afeta todo dossiê, não um domínio
isolado) — confirmado rodando `sseResponse()` de verdade contra os 273
dossiês reais antes do fix: todos os 273 mostravam o campo quebrado.

**Corrigido** em `dashboard/src/lib/sse.ts` (`normalizeTimestamps`,
único ponto por onde os dois streams `/api/stream/queue` e
`/api/stream/investigation/[domain]` passam) — converte qualquer
`Timestamp` do Firestore em qualquer profundidade do payload para string
ISO 8601 na leitura, sem tocar `orchestrator.py` e sem backfill dos
documentos já gravados. Reverificado rodando o `sseResponse()` real
contra os mesmos 273 dossiês depois do fix: **0 de 273** com campo de
data quebrado (`investigated_at` e `evidence.collected_at` corretos nos
273; nenhum outro campo `*_at` inválido em nenhum). Build (`next build`,
TypeScript incluído) limpo. Rebuild via `cloudbuild.yaml`
(`sentinel-dashboard:invalid-date-fix`) + `gcloud run deploy` — no ar,
confirmado por execução: `/review` sem sessão continua redirecionando
307 pro `/login` (proxy/auth intacto), `GOOGLE_CLIENT_ID` continua
embutido no bundle do cliente (checado no chunk JS servido, não só na
env var do Cloud Run).

### Limitação conhecida, documentada e NÃO corrigida (decisão deliberada) — sessão do dashboard

`decodeSession` (`dashboard/src/lib/session-cookie.ts`, usado tanto pelo
`proxy.ts` quanto por `getSession()`) valida só duas coisas: assinatura
HMAC e `expiresAt`. Isso significa:

- **Sem revogação por sessão.** `DELETE /api/session` (logout) só apaga o
  cookie no navegador — não existe sessão do lado servidor pra invalidar.
  Se um cookie vazar antes do logout, continua válido até expirar (12h),
  mesmo depois do "logout". Única forma de revogar tudo de uma vez é
  trocar `SESSION_SECRET` (derruba TODAS as sessões ativas, instrumento
  bruto).
- **Allowlist checada uma única vez.** `ALLOWED_REVIEWER_EMAILS`/
  `ALLOWED_REVIEWER_DOMAIN` só são avaliados em `verifyGoogleIdToken`, no
  login (`POST /api/session`). Depois que o cookie é emitido, mudar a
  allowlist não afeta sessões já emitidas até elas expirarem — não há
  recheck por request.
- **Sem nonce/jti.** Bearer token puro por 12h, sem tracking de uso único.

Decisão explícita de não mexer nisso agora — risco de auth a dois dias da
gravação não vale a pena; documentado aqui como limitação conhecida, não
como pendência de correção imediata.
