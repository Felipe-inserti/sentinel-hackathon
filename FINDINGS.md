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

## 13. Apagar o Firestore não limpa o Pub/Sub — dossiês fantasma no boot do run oficial (27/08/2026)

Mesmo padrão de outros achados desta sprint (`ct_last_index_processed`,
`observation_run.py` ausente da imagem): **estado distribuído em dois
sistemas, limpeza feita em um só.**

Antes de ligar `obs-2026-08-27`, apagamos os 10.227 documentos de
`investigations` e os 4 documentos de `observation_runs` (medições
descartáveis + o run acidental `obs-2026-08-26`) — Terraform/gcloud não
tocam dado do Firestore, então isso era uma limpeza só do Firestore, de
propósito. O que não consideramos: `sub-evidence` (Pub/Sub) tinha um
backlog real de milhares de mensagens `investigation-completed`
publicadas ANTES da limpeza (confirmado por execução: mensagem mais
antiga espiada tinha `publishTime=2026-08-27T15:59:26Z`, 3h18min antes do
boot do run oficial), nunca drenado porque o `evidence-collector-job` não
rodava fazia horas. Retenção de 24h do Pub/Sub — nada tinha expirado
ainda.

Quando o `evidence-collector-job` foi religado, ele processou essa fila
antiga primeiro (ordem de chegada no Pub/Sub, não por relevância). Para
cada mensagem cujo domínio já não existia mais em `investigations`
(porque apagamos), `_update_dossier_with_evidence`
(`evidence_agent.py`) grava com `doc_ref.set({...evidence...},
merge=True)` — que **cria o documento do zero** quando ele não existe.
Resultado: um dossiê `status=PENDING_HUMAN_REVIEW` só com os campos que o
evidence-collector escreve (`evidence`, `evidence_agent_id`,
`evidence_agent_version`, `status`) — sem `classification`,
`matched_brand`, `confidence`, `reasoning`, `injection_signals`, nada do
que `orchestrator.py` deveria ter escrito primeiro.

**Medido, não estimado**: de 423 documentos `PENDING_HUMAN_REVIEW` no
pico, **419 (99,05%) eram esses dossiês fantasma** — só 4 tinham dado de
investigação completo, e esses 4 eram genuinamente do run oficial (todos
com `investigated_at` depois de `19:17:46`, o boot do run).

**Risco real pro dashboard, não só sujeira de dado**:
`ReviewCard.tsx`/`review/[domain]/page.tsx` faziam
`investigation.injection_signals.length > 0` sem null-check —
`injection_signals` é um campo que só `orchestrator.py` grava, ausente em
todo dossiê fantasma. `undefined.length` lança exceção em runtime —
qualquer revisor abrindo `/review` com um fantasma na fila veria a
página quebrar, não só um card feio.

### Correção aplicada
- **Dashboard** (`dashboard/src/components/ReviewCard.tsx`,
  `dashboard/src/app/(app)/review/[domain]/page.tsx`): guarda
  `(investigation.injection_signals ?? []).length > 0` nos dois lugares
  — defesa contra QUALQUER documento malformado, não só fantasma
  especificamente. Build (`next build`) limpo, redeploy confirmado
  (`sentinel-dashboard-00004-mcz`, `/review` sem sessão continua
  redirecionando 307).
- **Dados**: `sub-evidence` esvaziado via `gcloud pubsub subscriptions
  seek` pro tempo atual (descarta backlog velho — a investigação
  original de cada domínio continua intacta em `observation_runs`/nos
  logs do orchestrator, só a evidência é adiada, nada se perde de
  verdade). Os 419 documentos fantasma apagados do Firestore (lista
  completa auditada antes de apagar — nenhum dos 4 dossiês reais do run
  oficial estava nela). `sub-orchestrator` conferido — **sem
  contaminação**, backlog vazio o tempo todo, `malicious_confirmed_total`
  do run oficial não é afetado por esse achado.

### Não corrigido ainda (decisão pendente, tamanho diagnosticado)
`_update_dossier_with_evidence` continua com o `merge=True` que fabrica
o documento. Devia checar existência (`doc_ref.update({...})`, que
lança `NotFound` de verdade quando o documento não existe, em vez de
`.set(merge=True)`, que nunca lança) e logar+descartar (ack, não nack —
reentregar não resolve dado que já foi apagado de propósito) em vez de
fabricar um dossiê fantasma. Tamanho: um método em `evidence_agent.py`
(troca de `.set()` por `.update()` + `try/except NotFound`, ~15-20
linhas) e um teste existente reescrito
(`tests/test_evidence_agent.py::test_update_dossier_with_evidence_merges_and_stamps_agent`,
hoje afirma literalmente `merge=True`) mais um teste novo pro caminho de
documento ausente. Pequeno e contido a um arquivo de produção + um de
teste, sem mudança de schema/infra — não implementado ainda, aguardando
aprovação explícita.

### Achado adicional: gargalo real de capacidade do evidence-collector

Motivado por uma pergunta de capacidade ("a janela de 1h, 2x/dia dá conta
do ritmo do run?") — medido depois da limpeza acima, sem contaminação:

| | Taxa medida |
|---|---|
| Produção de MALICIOUS (`orchestrator`, 2h08min de run real) | ~440-457/hora |
| Processamento do `evidence-collector` (~10min de janela limpa, duas amostras) | ~436-481/hora |

As duas taxas são **essencialmente iguais** — o evidence-collector, quando
ativo, mal acompanha a produção corrente, sem margem real pra drenar
backlog acumulado fora da janela. Causa raiz encontrada por leitura de
código, não suposição: `MAX_INFLIGHT_MESSAGES = 5` (`evidence_agent.py`)
limita o `FlowControl` do Pub/Sub a 5 mensagens simultâneas — com
Playwright + DNS + TLS + RDAP por domínio MALICIOUS (vários segundos
cada, mais falhas de rede reais observadas: `ERR_CONNECTION_REFUSED`,
`ERR_SSL_PROTOCOL_ERROR`, timeouts), 5 é pouco. `evidence-collector-job`
tem 2 vCPU / 2Gi hoje — subir a concorrência sem subir recurso arrisca
contenção de CPU/memória (Chromium não é leve), não necessariamente mais
throughput.

Com a cadência atual (`0 0,12 * * *`, 1h de janela, 2x/dia = 2h
ativo/24h) contra ~450/hora de produção contínua: produção diária ≈
10.800, capacidade diária ≈ 900 (2h × ~450/hora) — **deficit de ~9.900
MALICIOUS/dia não processados dentro da retenção de 24h do Pub/Sub**.
Não corrigido — decisão de ajuste (mais janelas, janela mais longa, subir
`MAX_INFLIGHT_MESSAGES` + recurso, ou aceitar perda parcial e documentar)
pendente, explicitamente não decidida sem aprovação.

## 14. IAM de observabilidade + prefixo real da métrica no Cloud Monitoring (Sprint 2, Stage A, 29/08/2026)

`roles/cloudtrace.agent`/`roles/monitoring.metricWriter` nas 5 SAs
(`ct-listener-sa`, `orchestrator-sa`, `evidence-sa`, `takedown-sa`,
`dashboard-sa`) já estavam aplicados de sessão anterior — `terraform
plan -target=...` (os 10 `google_project_iam_member` de trace/métrica em
`infra/main.tf`) deu **0 diff**, confirmado de forma independente contra
a IAM policy real (`gcloud projects get-iam-policy seu-id-unico`), não só
o state do Terraform.

Isso remove a CAUSA conhecida (falta de papel IAM) mas não prova, por si
só, que trace/métrica chegam — o histórico deste projeto já mostrou mais
de uma vez que "o código está correto" e "o dado chega no backend" são
coisas diferentes (ver item 11 acima). Prova exigida e obtida: publicada
uma mensagem real em `suspicious-domain-detected` com um `traceparent`
W3C gerado manualmente (`00-<trace_id>-<span_id>-01`), `orchestrator-job`
disparado manualmente (imagem antiga, pré-multimodal — não precisou do
Stage B), execução cancelada logo depois de confirmar o dossiê gravado
(timeout do Job é 7200s, não podia ficar rodando).

**Trace**: `GET https://cloudtrace.googleapis.com/v1/projects/{project}/traces/{trace_id}`
devolveu os 7 spans esperados (`cache.lookup`, `scrape.fetch`,
`brand_memory.inject`, `sanitize.clean`, `llm.analyze`,
`firestore.persist`, `pubsub.publish`), todos com `parentSpanId` igual ao
`span_id` que eu tinha injetado manualmente — confirmado por conversão
hex→decimal exata (`int("5c93a199b3c1759f", 16) ==
6670853154583704991`), não por inspeção visual. Prova de que o
`traceparent` atravessa o Pub/Sub pelo mecanismo real do código
(`telemetry.extract_context(message.attributes)`), não um trace
coincidente.

**Achado sobre o prefixo da métrica** (o que valia registrar aqui): as
métricas customizadas do OTel (`llm_invocations_total`,
`tokens_consumed_total`, `estimated_cost_usd_total`, etc., ver
`telemetry._COUNTER_NAMES`) **não** aparecem em Cloud Monitoring sob
`workload.googleapis.com/<nome>` (o prefixo "recomendado" pela doc geral
de ingestão OTLP — [OTLP metric ingestion overview](https://docs.cloud.google.com/stackdriver/docs/otlp-metrics/overview)).
Elas aparecem sob **`prometheus.googleapis.com/<nome>/counter`**, com
`resource.type = "prometheus_target"` e `resource.labels.location` =
`settings.otel_region` (`us-central1`). Motivo (confirmado contra a doc —
[v1.metrics reference](https://docs.cloud.google.com/stackdriver/docs/reference/telemetry/v1.metrics)):
o `Resource` que `telemetry.py` monta (sem indicadores de plataforma
GCE/GKE — roda em Cloud Run Job, que não se anuncia como tal nos
atributos padrão do `GoogleCloudResourceDetector` da forma que ativaria
um mapeamento de monitored-resource nativo) cai no caminho de mapeamento
Prometheus da Telemetry API, não no caminho `workload.googleapis.com`.
Consultar pelo prefixo errado (`workload.googleapis.com/...`) devolve
"Cannot find metric(s)..." — parece com métrica ausente/não exportada,
mas é só o prefixo errado. Comando de consulta correto documentado em
`docs/DEMO_COMMANDS.md`.

Valores conferidos batendo exato com a chamada de teste real:
`llm_invocations_total=1`, `tokens_consumed_total=690` (631 input + 59
output, mesmo número do log `llm_call ...`), `estimated_cost_usd_total=
0.0006945` (mesmo valor do span `llm.analyze`).

## 15. Tag mutável (`:latest`) + Terraform comparando string = deploy que "dá certo" e não deploya nada (Sprint 2, Stage C, 29/08/2026)

Achado ANTES de aplicar, ao investigar se `takedown-agent-job` precisava
de deploy junto com o `orchestrator-job` no mesmo apply.

`infra/cloud_run_jobs.tf` declara `image = var.agents_image` para
`ct_listener`, `orchestrator` (antes do Sprint multimodal) e
`takedown_agent` — e o valor passado é sempre a MESMA string de tag,
`us-central1-docker.pkg.dev/{project}/sentinel-images/sentinel-agents:latest`,
nunca um digest (`@sha256:...`). Confirmado por execução
(`gcloud run jobs describe takedown-agent-job --format="value(spec.template.spec.template.spec.containers[0].image)"`)
que o Job vivo também guarda essa mesma string sem digest.

**O problema**: Cloud Run resolve uma tag mutável para um digest no
momento em que o recurso do Job é criado/atualizado, não a cada
`execute`. Um `docker push` novo para a mesma tag depois disso não é
pego automaticamente por execuções seguintes do Job já existente — só um
`gcloud run jobs deploy`/`terraform apply` que efetivamente TOQUE aquele
recurso força a re-resolução. E como `terraform apply` decide se toca um
recurso comparando o VALOR do campo `image` no state contra o valor
planejado — string idêntica, `0 diff` — um apply que rebuilda a imagem
com `docker push` para a mesma tag e depois roda `terraform apply`
direcionado a OUTRO Job (ex: só `orchestrator`, que teve o campo `image`
de fato alterado para `var.orchestrator_image` neste sprint) relata
sucesso, sem erro nenhum, e **`takedown-agent-job` continua rodando o
binário antigo indefinidamente** — sem `_format_evidence_hashes_block`
neste caso especifico, mas o padrão vale para qualquer mudança futura
que só troque o conteúdo da imagem sem trocar a string da tag.

**Mesma família de falha** dos outros achados silenciosos deste projeto
(`PERMISSION_DENIED` de trace/métrica sem papel IAM, item 14 acima; o
sandbox reportando execução de testes que nunca rodaram, ver
`docs/PREFILTER_THRESHOLD_BEFORE_AFTER.md`/histórico do projeto): **o
sistema diz que deu certo, e não deu.** Aqui especificamente, nem existe
uma mensagem de erro para não ler — o Terraform genuinamente não tem
informação para saber que algo mudou, porque a informação (o digest) não
está no campo que ele compara.

**Correção aplicada neste sprint**: `terraform apply
-replace=google_cloud_run_v2_job.takedown_agent` no mesmo apply do
Stage C — força a recriação/atualização do recurso mesmo com `image`
textualmente igual, fazendo o Cloud Run re-resolver `:latest` para o
digest publicado agora. Verificação pós-apply obrigatória (não deduzida
do "apply completo com sucesso" do Terraform): comparar o digest
efetivamente em execução em CADA Job (`gcloud run jobs describe
--format="value(spec.template.spec.template.spec.containers[0].image)"`
mostra a tag; o digest resolvido de verdade fica no campo
`status.latestCreatedExecution` ou é obtido rodando uma execução e
inspecionando o container) contra o digest que o `docker push` reportou.

**Não corrigido, registrado para decisão futura**: isto não é um
problema só deste apply — é estrutural enquanto `agents_image`/
`orchestrator_image`/`evidence_image` continuarem sendo tags mutáveis em
vez de digests. `deploy.sh` (e este fluxo manual) sempre terão esse risco
em qualquer sprint futuro que rebuilde uma imagem sem mudar a tag. Opção
mais robusta para o futuro: `deploy.sh` capturar o digest resolvido do
`docker push`/`gcloud builds submit` e passar
`image = "...@sha256:..."` para o Terraform em vez da tag — mudança de
padrão que não foi decidida nem aplicada agora (fora do escopo deste
sprint, decisão de arquitetura de deploy que merece aprovação própria).

## 16. Domain-lock de `page_capture.py` recebia o campo `domain` cru, não o hostname navegado — bug real, exposto pela demo (Sprint 2, Stage D, 29/08/2026)

Achado ao montar o alvo público do Stage D (bucket GCS servindo
`demo/phishing-target/`, ver item 17 abaixo para por que precisou ser por
caminho/URL, não por domínio puro).

`classify_domain_with_gemini` (`plane2_agents/orchestrator.py`) chamava:
```python
screenshot_bytes = await page_capture.capture_page_screenshot(target_url, domain)
```
passando o campo `domain` (vindo direto de `SuspiciousDomainSignal.domain`
— `str` sem nenhuma restrição de formato no schema, ver
`seed_registry.py`) como `target_domain` da trava de navegação
(`page_capture._domain_lock_router`, compara contra o **hostname** de
cada requisição de navegação). Em produção real isso nunca quebrou porque
`domain` sempre chega como hostname puro (CN/SAN de certificado, via
`ct_listener.py`) — mas **nada no código garantia isso**, era uma
suposição implícita nunca testada.

Quando o alvo de teste do Stage D precisou de uma URL com path
(`https://bucket.storage.googleapis.com/malicious.html` — GCS só serve
`mainPageSuffix` atrás de um Load Balancer com domínio próprio, que não
temos, ver item 17), a suposição quebrou: `domain` = a URL inteira com
path, o hostname real da navegação (`bucket.storage.googleapis.com`)
nunca bate contra essa string completa, e a trava **abortava até a
navegação inicial legítima** — falso positivo de segurança, não um
detalhe de teste. Se um payload real algum dia chegasse com `domain`
contendo um path por qualquer motivo (bug em outro produtor, replay
malformado), a captura de screenshot pararia de funcionar silenciosamente
(fail-safe do `page_capture.py` trataria isso como falha de captura, sem
erro visível) — comportamento errado, mas não catastrófico, por causa do
fail-safe já existente.

**Corrigido** em `classify_domain_with_gemini`: o parâmetro de trava
agora vem de `urlparse(target_url).hostname` (a URL efetivamente
navegada), nunca do campo `domain` cru. `page_capture.py` (incluindo
`_domain_lock_router`) **não mudou uma linha** — mesma trava, mesma
função, só quem a chama passa um argumento derivado corretamente. Os 3
testes existentes (`tests/test_page_capture.py`) continuam passando sem
nenhuma alteração (confirmado por diff vazio antes de rodar). Teste
adversarial novo (`tests/test_orchestrator_capture_domain_lock.py`, 3
casos): URL com path + redirect pra outro host → bloqueia; URL com path +
navegação no mesmo host → permite; URL sem path (caso de produção hoje) →
comportamento idêntico ao de antes da correção. Suíte completa: 329→332
passed, 3 deselected, zero regressão.

## 17. `mainPageSuffix`/website hosting do GCS não funciona sem Load Balancer + domínio próprio (Sprint 2, Stage D, 29/08/2026)

`infra/demo_target_bucket.tf` foi escrito com `website { main_page_suffix
= "index.html" }`, assumindo (nunca verificado antes de aplicar) que isso
serviria `index.html` na raiz do bucket via `<bucket>.storage.googleapis.com/`.
**Não serve.** Confirmado por execução (`curl` real contra o bucket já
aplicado, com `index.html` de fato presente) e contra a documentação
oficial ([Cloud Storage — hosting a static website](https://docs.cloud.google.com/storage/docs/hosting-static-website)):
"without a custom domain and load balancer, users who access your
top-level site are served an XML document tree containing a list of the
public objects in your bucket." `mainPageSuffix` só tem efeito atrás de
um Application Load Balancer com domínio verificado (CNAME) — inacessível
neste projeto (sem domínio próprio). O bloco `website{}` no `.tf`
permanece (documentado como inerte, ver comentário no arquivo) em vez de
removido, para não perder a intenção registrada caso um Load Balancer
seja adicionado no futuro.

**Resolução aplicada**: alvo público servido por URL de caminho
(`https://<bucket>.storage.googleapis.com/<arquivo>.html`, funciona sem
LB nenhum) em vez de domínio puro na raiz — o que expôs o achado do item
16 acima. Para alternar qual variante o orchestrator classifica, o objeto
`index.html` do bucket é sobrescrito manualmente antes de cada teste
(`gcloud storage cp <variante>.html gs://.../index.html`) e acessado via
`https://<bucket>.storage.googleapis.com/index.html` — path explícito,
não a raiz "mágica" (que continuaria devolvendo a listagem XML mesmo com
`index.html` presente, exatamente como descrito acima).

## 18. `domain` com `/` quebra o Firestore (não só o domain-lock) — e a falha é 100% silenciosa (Sprint 2, Stage D, 29/08/2026)

Achado ao rodar o D.1/D.2 de verdade contra o alvo do bucket (URL por
caminho, ver item 17): publiquei a mensagem real em
`suspicious-domain-detected` com
`domain="seu-id-unico-sentinel-demo-target.storage.googleapis.com/index.html"`,
disparei o `orchestrator-job`, e **nada aconteceu** por 5 minutos — só o
log de startup (`Orchestrator escutando em ...`), nenhum `CACHE MISS`,
nenhum erro, nenhum log de qualquer tipo. `num_undelivered_messages` da
subscription `sub-orchestrator` continuava em 1 o tempo todo -- a
mensagem nunca foi confirmada (ack), mas também nunca gerou NENHUM sinal
de falha visível em lugar nenhum (Cloud Logging incluso).

**Causa raiz, reproduzida localmente**:
```python
>>> db.collection("investigations").document("bucket.storage.googleapis.com/index.html")
ValueError: A document must have an even number of path elements
```
`google-cloud-firestore` interpreta `/` dentro da string passada a
`.document()` como SEPARADOR DE CAMINHO (coleção/documento/subcoleção/...),
não como parte literal do ID. `_get_cached_investigation`/`_save_investigation`
(`plane2_agents/orchestrator.py`) usam `domain` diretamente como ID de
documento em `investigations/{domain}` — nunca sanitizado, porque em
produção real `domain` é sempre um hostname puro (CN/SAN de certificado,
sem `/`), então isso nunca apareceu antes. Mesma categoria de suposição
implícita do achado #16 (campo `domain` tratado como "sempre hostname
puro" em múltiplos lugares do código, nunca validado).

**O que torna isto mais sério que o achado #16**: a falha é
**completamente silenciosa**. `_handle_pubsub_message._process()`
(`plane2_agents/orchestrator.py`, linha ~890-898):
```python
async def _process() -> None:
    token = otel_context.attach(extracted_ctx)
    try:
        await investigate_domain(domain, matched_brand, agent_manifest, detected_at)
        message.ack()
    except Exception:
        message.nack()
    finally:
        otel_context.detach(token)
```
`except Exception: message.nack()` -- **sem nenhum `logger.exception`/
`logger.error` antes do nack**. Uma `ValueError` do Firestore (ou
QUALQUER outra exceção não prevista em `investigate_domain`) é capturada,
a mensagem é recusada (nack, Pub/Sub tenta de novo), e **nenhum rastro
fica em lugar nenhum** -- nem log, nem métrica de erro, nem span marcado
como falho. Mesma família dos achados #14 (trace/métrica sem IAM) e #15
(tag mutável): o sistema não avisa que algo deu errado, só fica quieto. A
diferença aqui é que nem sequer existe uma mensagem de erro para
descobrir depois -- é preciso reproduzir localmente pra achar a causa.

**Consequência prática para o Stage D**: a estratégia de usar `domain`
contendo o path do objeto GCS (`bucket/index.html`) não é viável --
quebra o Firestore, não só a trava de `page_capture.py` (achado #16, já
corrigido). Isso exige repensar como o alvo público é servido, não só
mais um ajuste de parâmetro. Opções levantadas, nenhuma aplicada ainda:
(a) um Cloud Run Service minúsculo servindo os arquivos, que dá um
hostname público de verdade SEM path (`https://servico-hash.a.run.app`,
mesmo padrão que `agent-gateway` já usa neste projeto) -- mais alinhado
com como um domínio real se parece, corrige os achados #16/#18 na raiz em
vez de contorná-los; (b) Load Balancer + domínio (`nip.io` ou
equivalente) na frente do bucket GCS -- mais pesado, ainda não
resolvido. Decisão pendente, não tomada nesta sessão sem aprovação.

**Não corrigido ainda (decisão pendente)**: a falha silenciosa em si
(`except Exception: message.nack()` sem log) é um problema de robustez
geral, independente do Stage D -- qualquer exceção inesperada em
`investigate_domain` (não só esta) desaparece sem rastro hoje. Correção
mínima proposta (não aplicada): `logger.exception(...)` antes do
`message.nack()`. Fica registrado para decisão explícita, mesmo padrão
de outros achados desta sessão que não foram corrigidos sem aprovação.

**Atualização (dispatch manual D.1-D.4, 29/08/2026)**: a correção mínima
proposta acima FOI aplicada, no mesmo commit (`4cc2f48`) que fechou este
achado -- ver achado #20 abaixo. O texto acima fica como estava escrito no
momento (histórico do diagnóstico), não editado.

## 19. Imagem `sentinel-orchestrator:latest` em produção está desatualizada -- não tem os achados #16/#18 (dispatch manual D.1-D.4, 29/08/2026)

Descoberto ao publicar as 3 primeiras mensagens de teste do dispatch D.1-D.4
com `domain` contendo path
(`sentinel-demo-target-....run.app/malicious.html`, forma que já deveria
funcionar depois do achado #18 estar corrigido no código): o
`orchestrator-job` consumiu as mensagens e **travou em silêncio** --
nenhum log novo por 5+ minutos, as 3 mensagens nunca confirmadas
(`num_undelivered_messages` parado em 3 o tempo todo). Exatamente o
sintoma já documentado no achado #18, reproduzido ao vivo.

**Causa raiz confirmada por execução** (não suposição): comparei a imagem
efetivamente rodando contra o código commitado.

```bash
gcloud run jobs describe orchestrator-job --project=seu-id-unico --region=us-central1 \
  --format="value(spec.template.spec.template.spec.containers[0].image)"
# us-central1-docker.pkg.dev/seu-id-unico/sentinel-images/sentinel-orchestrator:latest

gcloud artifacts docker images list \
  us-central1-docker.pkg.dev/seu-id-unico/sentinel-images \
  --include-tags --filter="package:sentinel-orchestrator"
# CREATE_TIME/UPDATE_TIME: 2026-08-29T00:28:47Z
# DIGEST: sha256:cd5c1313c323ca98e5fa2683f8b7c1aae7bc0bd69e52b212e9ee0f05277bbd0b
```

O commit `4cc2f48` (que introduz `Dockerfile.orchestrator`,
`plane2_agents/page_capture.py`, e as correções dos achados #16/#18) tem
`AuthorDate: Sat Aug 29 02:30:00 2026 -0300` = **05:30:00 UTC** -- **5
horas depois** do build da imagem (`00:28:47 UTC`). A imagem em produção
foi construída a partir de um estado LOCAL intermediário do sprint
multimodal (já tinha `page_capture.py`/captura de screenshot funcionando
-- confirmado, os 3 dispatches de teste classificaram com
`visual_analysis_available=True`), mas **antes** das últimas correções
terem sido escritas.

**Confirmado por extração direta do código rodando** (sem rebuild --
`gcloud run jobs execute` com `--args` sobrescrevendo só os args do
container, comando `python`, para fazer a própria imagem imprimir seus
arquivos via stdout/Cloud Logging):

```bash
gcloud run jobs execute orchestrator-job --project=seu-id-unico --region=us-central1 \
  --args="-c,import pathlib;import sys;sys.stdout.write(pathlib.Path('/app/plane2_agents/orchestrator.py').read_text())" \
  --async
# depois: gcloud logging read '...execution_name="<nome>"...' --order=asc
```

Comparando o dump real (818 linhas) contra `plane2_agents/orchestrator.py`
do HEAD atual (987 linhas) com `diff -u -B` (ignorando diferenças de linha
em branco -- o pipe de logging do Cloud Run não preserva linhas vazias
como entradas distintas), a imagem em produção **não tem** nenhuma das 3
correções abaixo -- e não tem MAIS NADA além delas (o resto de
`orchestrator.py`, e `telemetry.py` inteiro exceto um nome de contador, são
idênticos):

1. `capture_lock_domain = urlparse(target_url).hostname or domain` (achado
   #16) -- a imagem em produção ainda chama
   `page_capture.capture_page_screenshot(target_url, domain)` com o campo
   `domain` cru.
2. `_firestore_safe_document_id()` (achado #18, parte 1) -- a imagem em
   produção ainda faz
   `db.collection(...).document(domain)` direto, sem sanitizar `/`.
3. O span `pubsub.process_message` + `logger.exception(...)` antes do
   `message.nack()` (achado #18, parte 2, "log de exceção") -- a imagem em
   produção ainda tem só
   `except Exception: message.nack()`, sem log, sem span, sem contador
   `investigate_domain_errors_total`. **Esta ausência é a causa raiz do
   achado #20 abaixo** (trace incompleto).

**Ação tomada nesta sessão**: cancelei a execução travada
(`orchestrator-job-jvr9x`) e descartei (`gcloud pubsub subscriptions pull
sub-orchestrator --auto-ack`) as 3 mensagens envenenadas -- sem essa
limpeza elas ficariam re-tentando indefinidamente (`sub-orchestrator` não
tem dead-letter policy configurada). Contornei o teste publicando `domain`
sempre como hostname puro (usando `SERVE_AS_ROOT` do
`sentinel-demo-target` para trocar o conteúdo servido na raiz, em vez de
usar path na URL) -- funciona contra a imagem antiga sem depender do
achado #18 estar corrigido nela.

**Rebuild mínimo necessário** (não executado nesta sessão -- só
diagnóstico, por pedido explícito): rebuild + push da MESMA tag
(`sentinel-orchestrator:latest`) a partir do `Dockerfile.orchestrator`
atual + `terraform apply -replace=google_cloud_run_v2_job.orchestrator`
(necessário por causa do achado #15 -- tag mutável, Terraform não percebe
sozinho que o conteúdo mudou). Nenhum outro arquivo copiado pela imagem
(`config.py`, `llm_client.py`, `registry.py`, `sanitizer.py`,
`brand_agent.py`, `brand_memory.py`, `observation_run.py`,
`plane1_ingestion/`) diverge do HEAD atual -- confirmado que
`requirements.txt`/`Dockerfile.orchestrator`/`page_capture.py` não
mudaram desde o commit que já está na imagem (`git show --stat 4cc2f48`
mostra os 4 arquivos tocados: `Dockerfile.orchestrator`,
`plane2_agents/orchestrator.py`, `plane2_agents/page_capture.py`,
`telemetry.py` -- os dois primeiros criados do zero nesse commit, ou
seja, já estavam completos quando a imagem foi buildada; só
`orchestrator.py`/`telemetry.py` têm o delta acima). Escopo do rebuild:
recompilar a imagem inteira (não há como trocar um arquivo isolado num
Cloud Run Job), mas o CONTEÚDO que muda de fato é só esse.

## 20. Trace incompleto no Cloud Trace -- regressão causada pela ausência do span `pubsub.process_message` na imagem em produção (dispatch manual D.1-D.4, 29/08/2026)

**Sintoma**: consultando os 3 `trace_id` reais dos dispatches de teste
(D.1/D.2/D.3, via `curl` contra
`https://cloudtrace.googleapis.com/v1/projects/seu-id-unico/traces/<id>`,
inclusive esperando ~2,5min/15 tentativas para descartar atraso de
propagação) -- cada trace devolve **1 único span** (`llm.analyze`),
aparecendo como **RAIZ** (`parentSpanId=None`). Os spans
`pubsub.process_message`, `cache.lookup`, `registry.invoke`,
`scrape.fetch`, `visual.capture`, `brand_memory.inject`, `sanitize.clean`,
`firestore.persist` nunca aparecem. Confirmado que não é atraso de
propagação comparando logs: a linha `CACHE MISS` (deveria estar dentro do
span pai) não carrega `logging.googleapis.com/trace` nenhum, enquanto as
linhas do `llm_client`/`httpx` (dentro do span `llm.analyze`) carregam
corretamente -- ausência sistemática, não atraso.

**Diagnóstico pedido explicitamente ANTES de qualquer correção** -- 4
hipóteses descartadas por execução real, não suposição:

1. `telemetry.setup()` é chamado no caminho novo? **Sim, nas duas
   imagens** -- `tracer = telemetry.setup("sentinel-orchestrator")` no
   nível de módulo de `orchestrator.py` (linha 87 do HEAD atual, linha 73
   do dump extraído da imagem em produção -- idêntico nas duas).
2. `extract_context` do Pub/Sub roda? **Sim, nas duas** -- código idêntico
   em `_handle_pubsub_message`. `extracted_ctx = telemetry.extract_context(message.attributes)`
   com um carrier vazio (mensagens publicadas via `gcloud pubsub topics
   publish` não carregam `traceparent`) devolve um `Context()` vazio, por
   design -- inicia um trace novo, comportamento esperado e documentado
   no próprio comentário do código.
3. O exporter é criado antes ou depois dos spans do pipeline? **Não é a
   causa** -- `_try_build_span_processor()` roda dentro de
   `telemetry.setup()`, sempre antes de qualquer span do pipeline ser
   criado (setup é chamado no import do módulo, antes de
   `run_orchestrator()` processar qualquer mensagem), idêntico nas duas
   imagens.
4. Exceção silenciosa no setup de telemetria? **Não** -- `_tracer` fica
   corretamente populado nas duas imagens (`_JsonTraceFormatter` mostra
   `logging.googleapis.com/trace` válido nos logs de `llm.analyze` em
   ambas). `telemetry.py` é idêntico entre as duas imagens exceto pela
   ausência de um nome de contador (`investigate_domain_errors_total`) na
   tupla `_COUNTER_NAMES` da imagem antiga -- não afeta tracer/span
   processor.

**Causa raiz real, confirmada por reprodução direta** (não pela
comparação de código sozinha): rodei `plane2_agents.orchestrator` LOCAL
(código do HEAD atual, `.venv`) contra a subscription `sub-orchestrator`
REAL do projeto, com um `SpanProcessor` de diagnóstico anexado ao
`TracerProvider` real (registra nome/trace_id/span_id/parent de cada span
no exato momento em que abre/fecha) -- publiquei uma mensagem real e
observei. Com o código do HEAD ATUAL, os 9 spans do pipeline (
`pubsub.process_message` → `cache.lookup` → `scrape.fetch` →
`visual.capture` → `brand_memory.inject` → `sanitize.clean` →
`llm.analyze` → `firestore.persist` → `pubsub.publish`) saem **todos com
o MESMO trace_id, todos com `parent=<span_id de pubsub.process_message>`,
todos na mesma thread** -- trace completo e corretamente aninhado. (Só
`registry.invoke`, que roda ANTES de `_process()` na thread do callback do
Pub/Sub, fica de fora por desenho -- span isolado, comportamento
documentado e esperado.)

A imagem em produção **não tem o span `pubsub.process_message`** (achado
#19, item 3): `_process()` chama `investigate_domain(...)` DIRETO, sem
nenhum `with tracer.start_as_current_span(...)` ao redor. Isso quebra o
encadeamento inteiro -- sem um span "guarda-chuva" vivo durante toda a
chamada, CADA `with tracer.start_as_current_span(...)` de nível mais alto
dentro de `investigate_domain`/`classify_domain_with_gemini`
(`cache.lookup`, `scrape.fetch`, `visual.capture`, `brand_memory.inject`,
`sanitize.clean`, `llm.analyze`, `firestore.persist`) abre e FECHA sem
nenhum pai ativo -- ao fechar, o contexto volta para o `Context()` vazio
anexado por `otel_context.attach(extracted_ctx)`, não para um span pai. O
PRÓXIMO span aberto (ex: `scrape.fetch`, depois que `cache.lookup` já
fechou) não tem mais nada para se pendurar, e vira uma **trace nova e
isolada, com seu próprio trace_id aleatório**. `llm.analyze` é só o
último da cadeia a abrir -- por isso aparece como raiz de sua própria
trace, desconectado de tudo que veio antes. Os outros spans
(`cache.lookup`, `scrape.fetch` etc.) muito provavelmente TAMBÉM foram
exportados para o Cloud Trace com sucesso -- só que cada um sob um
trace_id diferente, nunca capturado nesta sessão (não há como descobri-los
a posteriori sem uma correlação de log, que também está ausente para
eles).

**Não é regressão de comportamento nem de outro código** -- é
consequência direta e integral do achado #19 (imagem desatualizada): o
span `pubsub.process_message` já existe no código commitado (parte da
correção "log de exceção" do achado #18), só nunca foi deployado.
Confirmado que a arquitetura de trace do Stage A (anterior a este sprint,
rodando `sentinel-agents:latest`, imagem sem o código multimodal) tinha o
trace completo -- esse worker antigo (`ct_listener.py`) sempre teve um
span guarda-chuva equivalente ao redor do processamento da mensagem.

**Correção**: nenhuma aplicada nesta sessão (só diagnóstico, por pedido
explícito). É o MESMO rebuild do achado #19 -- a correção já existe no
código commitado, só precisa ser deployada.

## 21. `injection-css-generated.html` não isola "só a imagem detecta" -- o que a imagem muda é a QUALIDADE DA EVIDÊNCIA, não o rótulo (dispatch manual D.1-D.4, 29/08/2026)

A intenção documentada no próprio arquivo (`demo/phishing-target/injection-css-generated.html`,
comentário do CSS) era demonstrar "prova real de 'veredito muda por causa
da imagem'" -- a hipótese: sem a captura de tela, o payload de injeção
(`::before{content:...}`, invisível a `BeautifulSoup.stripped_strings`,
ver achados de validação anteriores) não é visto, e o modelo classificaria
diferente.

**Testado com o Gemini real** (chamada direta a
`classify_domain_with_gemini`, mesmo conteúdo, uma vez com a captura de
tela normal e outra com `page_capture.capture_page_screenshot` forçado a
devolver `None`):

| | COM imagem | SEM imagem |
|---|---|---|
| classification | MALICIOUS | MALICIOUS |
| confidence | 1.00 | 0.99 |
| visual_analysis_available | True | False |
| reasoning cita o payload de injeção? | Sim -- `text_in_image_summary`: "...contendo texto de manipulação direcionado ao analisador..." | Não -- o modelo nunca soube que a tentativa existiu |

**O rótulo não muda.** A página tem marca (`BancoTeste`) + formulário de
credencial (CPF/senha/código de verificação) visíveis no HTML normal
(nós de DOM reais, capturados por `stripped_strings` independente da
imagem) -- sinal suficiente, sozinho, para o Gemini classificar MALICIOUS
com ou sem a imagem (mesmo padrão de `malicious.html`, que também dá
MALICIOUS 1.00 e não tem nenhum payload de injeção). A variável "só a
imagem detecta" não está isolada nesta página -- para isolar de verdade,
precisaria de uma variante SEM marca/formulário visíveis no HTML, só o
payload CSS-gerado, e testar se o modelo classifica SAFE sem imagem e
MALICIOUS com imagem.

**O que a imagem de fato muda, e é real**: a QUALIDADE/especificidade da
evidência. Sem imagem, o modelo acerta o rótulo por um motivo genérico
(marca + formulário) sem nunca saber que houve uma tentativa de
manipulação do próprio analisador. Com imagem, o modelo vê e CITA
explicitamente o payload de injeção como evidência -- a diferença entre
"acertar por sorte/heurística geral" e "detectar e neutralizar o ataque
de verdade", visível no campo `text_in_image_summary` e no `reasoning`.
Vale mencionar isso com essa precisão em qualquer material de demo -- não
como "a imagem muda o veredito", que não é o que os dados mostram para
este arquivo específico.

## 22. Regra operacional: `terraform apply -replace` num Cloud Run Job exige um SEGUNDO apply isolado para restaurar o IAM binding (rebuild do orchestrator-job, 29/08/2026)

Descoberto ao aplicar o rebuild que corrige os achados #16/#18/#19: rodei
`terraform apply -replace=google_cloud_run_v2_job.orchestrator
-target=google_cloud_run_v2_job.orchestrator
-target=google_cloud_run_v2_job_iam_member.scheduler_invoke_orchestrator`
NUM ÚNICO apply, com os dois `-target` juntos, esperando que o binding de
IAM saísse correto no final. **Não saiu.** Confirmado por execução
imediatamente depois:

```bash
gcloud run jobs get-iam-policy orchestrator-job --project=seu-id-unico --region=us-central1
# etag: BwZaKbIisQU=   <- SEM bindings nenhum

gcloud run jobs get-iam-policy takedown-agent-job --project=seu-id-unico --region=us-central1
# bindings: [{ role: roles/run.invoker, members: [scheduler-sa@...] }]  <- correto, referencia
```

**Mecanismo, agora compreendido** (não só observado -- reproduzido e
explicado): um `terraform plan`/`apply` calcula TODO o plano de uma vez,
contra o estado JÁ REFRESCADO no início da chamada -- antes de qualquer
ação de destroy/create ser executada. Quando o plano inclui tanto o
`-replace` do Job quanto o `-target` do `google_cloud_run_v2_job_iam_member`
correspondente, o binding de IAM é avaliado contra o Job **ainda
existente, com a política antiga intacta** -- por isso o plano mostra "0
to change" para o binding, mesmo sabendo que o Job vai ser destruído
poucos segundos depois na mesma execução. O Cloud Run recria o Job do
zero (novo `uid`, gerado como recurso novo, não um update in-place) e a
política de IAM não sobrevive à destruição do recurso original -- mas
Terraform nunca reavalia o binding DEPOIS da recriação, porque isso só
aconteceria numa chamada de `plan`/`apply` seguinte.

**Confirmado que a correção funciona**: um SEGUNDO `terraform plan
-target=google_cloud_run_v2_job_iam_member.scheduler_invoke_orchestrator`
(sozinho, sem `-replace`, DEPOIS do apply que recriou o Job) refrescou o
estado contra o Job JÁ NOVO e corretamente detectou `1 to add` (binding
ausente na realidade) -- `terraform apply` desse segundo plano restaurou
o binding, confirmado de novo por `get-iam-policy` idêntico ao do
`takedown-agent-job`.

**Esta é a MESMA causa raiz do incidente anterior com `takedown-agent-job`**
(mencionado em conversa, não documentado por escrito até agora) --
aconteceu duas vezes porque a causa é estrutural do par
`google_cloud_run_v2_job` + `google_cloud_run_v2_job_iam_member` como dois
recursos Terraform separados, não um bug pontual de nenhum dos dois
sprints.

**Regra operacional, registrada em `infra/README.md`** (seção "Uso —
`deploy.sh`/`teardown.sh`", ver lá): todo `-replace` de um
`google_cloud_run_v2_job` (qualquer um dos 4 workers) precisa ser seguido
por um `terraform apply` SEPARADO, sem `-replace`, alvo (`-target`) no(s)
`google_cloud_run_v2_job_iam_member` daquele Job -- nunca confiar que
incluir os dois `-target` no MESMO apply é suficiente. Verificação
obrigatória depois de qualquer `-replace`: `gcloud run jobs get-iam-policy
<job> --project=... --region=...` tem que devolver os `bindings`
esperados, não só "apply completo com sucesso" (mesma família dos achados
#14/#15/#18 -- o sistema não avisa quando fica sem o binding, só fica
quieto até o Scheduler tentar invocar e falhar por permissão).

## 23. Documento de cache parcial derruba `investigate_domain` em loop infinito de retry -- `sub-orchestrator` não tem dead-letter policy (30/08/2026, ensaio pré-gravação)

Descoberto ao ensaiar a Cena 1 (publish manual de
`suspicious-domain-detected` contra `sentinel-demo-target-...run.app`,
domínio já usado antes para preparar a Cena 3 do vídeo). Toda tentativa
resultava em:

```
CACHE HIT para sentinel-demo-target-cugvqtrd7q-uc.a.run.app (economia de 100% de tokens)
Falha inesperada processando investigacao de ... -- mensagem NACK'd para retry (Pub/Sub)
```

com dois `trace_id` diferentes confirmados em tentativas sucessivas
(`eb48dee7a4fa2c8a473bfb7e517a9a45`, `c75fe21c07a89d8ed1bb0f0744fd7283`)
-- ou seja, não era uma trava presa numa única mensagem, era redelivery
real e repetido.

### Causa raiz

`investigations/sentinel-demo-target-cugvqtrd7q-uc.a.run.app` já existia
no Firestore, mas só com os campos `evidence`/`evidence_agent_id`/
`evidence_agent_version`/`status=PENDING_HUMAN_REVIEW` -- sem
`classification`/`confidence`. Alguma preparação anterior da Cena 3 (fora
do caminho normal do orchestrator) escreveu esse documento parcial direto
no Firestore para deixá-lo pronto para aprovação no dashboard, pulando a
etapa de classificação. `plane2_agents/orchestrator.py:738` assume que
todo documento em cache tem `classification`:

```python
_publish_completed(domain, cached["classification"], cached["confidence"], cache_hit=True)
```

`cached["classification"]` -> `KeyError` antes mesmo de `_publish_completed`
rodar (nunca chegou a publicar `investigation-completed` por esse
caminho). A leitura de cache (`_get_cached_investigation`) não valida que
o documento tem o schema mínimo esperado antes de tratá-lo como "já
investigado" -- qualquer documento parcial em `investigations/{domain}`
(seedado manualmente, ou uma escrita futura que grave campos incompletos
por qualquer motivo) provoca o mesmo crash.

`sub-orchestrator` não tem `deadLetterPolicy` configurada (confirmado via
`gcloud pubsub subscriptions describe`) -- só `retryPolicy` (backoff
10s–60s). Sem DLQ, uma mensagem que sempre falha do mesmo jeito **não sai
de circulação sozinha**: fica sendo redelivered indefinidamente (o
processamento em si é barato -- 1 leitura de Firestore por tentativa,
falha antes de qualquer chamada ao Gemini -- mas é um vazamento de
"lixo" operacional que só um humano ou um DLQ resolve).

### Correção aplicada nesta sessão (contorno, não a causa raiz)

Para desbloquear o ensaio: apaguei o documento parcial
(`investigations/sentinel-demo-target-...run.app`) e fiz
`gcloud pubsub subscriptions seek sub-orchestrator --time=<agora>` para
descartar a mensagem em loop. Republicar depois disso produziu o fluxo
completo e correto: `CACHE MISS` -> scraping real -> Gemini
(`gemini-3.5-flash-lite`, 2892 input / 219 output tokens, US$0,00299025,
2076ms) -> `MALICIOUS` (confiança 1.0, `credential_form_present=True`,
`visual_brand_match=True`) -> evidence-collector real (fingerprint de
infra, análise visual) -> `PENDING_HUMAN_REVIEW`. `trace_id`:
`eadca04100a6e23ccaa72aff792e8f70`.

### Duas lacunas reais, registradas por decisão explícita para NÃO corrigir agora

Deliberado: sprint aditivo sob pressão de prazo de gravação, correção de
causa raiz fica para depois.

1. **`_get_cached_investigation`/`investigate_domain` deveriam validar o
   schema do documento em cache antes de confiar nele** (ex: checar que
   `classification`/`confidence` existem, tratar ausência como cache
   miss em vez de deixar o `KeyError` estourar) -- um documento parcial
   nunca deveria conseguir travar o pipeline inteiro.
2. **`sub-orchestrator` deveria ter uma `deadLetterPolicy`** (como as
   outras filas já deveriam considerar) -- sem isso, qualquer mensagem
   "envenenada" (poison message) circula para sempre em vez de ser
   isolada para inspeção manual depois de N tentativas.

### Achado adicional, apenas observado: `matched_brand="bancoteste"` não é uma marca seedada

`registry.get_agent` rejeitou `brand-agent-bancoteste` ("nenhuma versão
ACTIVE") -- só `nubank`/`loggi`/`ifood` estão seedados
(`seed_brand_agents.py`, ver CLAUDE.md). A investigação seguiu
normalmente sem contexto de marca (`brand_agent_id=None`), como
projetado -- mas isso significa que testes com este domínio de demo nunca
exercitam o caminho de `BrandAgent`/`brand_memory`. Não é bug, é só uma
lacuna de cobertura do ensaio, registrada para quem for narrar a Cena 1
saber que o "few-shot de marca" não está em jogo nesta demo específica.
