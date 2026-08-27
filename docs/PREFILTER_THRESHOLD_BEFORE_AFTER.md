# Prefiltro: evidência "antes" (`max_edit_distance=2`) preservada antes da limpeza do run oficial

Este arquivo existe só para não perder a prova viva do bug de colisão de
dicionário já descrito em [`FINDINGS.md`](../FINDINGS.md) item 10, antes
de apagar os dados que o produziram (limpeza pré-`obs-2026-08-27`, ver
`docs/RED_TEAM.md`/git log do dia). Os exemplos abaixo vêm de
`observation_runs/obs-2026-08-26` — confirmado por execução (ver histórico
da sessão) que essa contagem rodou com **código antigo**:
`DEFAULT_MAX_EDIT_DISTANCE=2`, sem `plane1_ingestion/ct_rfc6962.py`, ainda
sobre certstream — não o código com o fix aplicado nesta sprint
(`max_edit_distance=1` + correção do bug de combinação de sinais, ver
`FINDINGS.md` item 10). Todos os documentos-fonte (`observation_runs/*` e
`investigations/*`) foram apagados logo depois desta captura, junto com o
resto da coleção de teste, pra começar `obs-2026-08-27` limpo.

## Linha de base — contadores dos 4 runs de teste (antes de apagar)

| run_id | started_at (UTC) | certs ingeridos | descartados prefiltro | LLM invocations | malicious confirmed | custo total (US$) |
|---|---|---|---|---|---|---|
| `obs-medicao-2026-08-27` | 2026-08-27 13:51:52 | 450.247 | 421.735 (93,67%) | 2.096 | 479 | 1,8552 |
| `obs-medicao-2026-08-27-v2` | 2026-08-27 15:08:34 | 1.001 | 993 (99,20%) | — | — | — |
| `obs-medicao-2` | 2026-08-27 15:29:22 | 378.506 | 376.295 (99,42%) | 157 | 40 | 0,1612 |
| `obs-2026-08-26` | 2026-08-27 13:51:54 | (não rodou ingestão — só drenou Pub/Sub do backlog acima, código antigo) | — | 7.848 | 1.775 | 7,1248 |

`obs-medicao-2` e `obs-medicao-2026-08-27-v2` já rodaram depois do fix de
`DEFAULT_MAX_EDIT_DISTANCE` (93,67%→99,2-99,4% de descarte, bate com o
92% de redução de volume medido em `FINDINGS.md` item 10). `obs-2026-08-26`
não fez ingestão nova — só o orchestrator/evidence-collector/takedown-agent
rodando em cron, drenando `sub-orchestrator`/`sub-evidence` com o código
**antigo** (por isso os exemplos de colisão abaixo vêm dele).

## Colisão de dicionário — falso positivo do `max_edit_distance=2` antigo

Cinco casos reais, mesmo padrão já registrado em `FINDINGS.md` item 10
("ifood"/"loggi" colidem com palavras comuns do inglês/português) — aqui
com o **reasoning completo do Gemini**, não só o veredito:

### `toledomudlogging.com` → matched_brand: **loggi** (confiança 0,95)
> O dominio toledomudlogging.com apresenta alta similaridade com a marca
> Loggi (phishing/typosquatting) e exibe uma pagina generica de "Coming
> Soon" do WordPress sem relacao legitima com a marca imitada.

Realidade: "mud logging" é termo de perfuração de poços de petróleo/gás —
nada a ver com a Loggi. Colisão pura na substring "logging".

### `autoconfig.toledomudlogging.com` → matched_brand: **loggi** (confiança 0,95)
> O dominio 'autoconfig.toledomudlogging.com' utiliza o sufixo 'logging'
> para se aproveitar de typosquatting ou confusao com a marca 'loggi',
> oferecendo configuracoes de e-mail automatizadas (autodiscover/
> autoconfig) que sao tipicamente usadas em campanhas de phishing
> corporativo ou coleta de credenciais.

Mesmo domínio-raiz do caso acima — subdomínio de autoconfig de e-mail
(padrão Microsoft/Outlook), não relacionado à Loggi.

### `interoperator.traefik.logging.sapcloud.io` → matched_brand: **loggi** (confiança 0,85)
> O dominio interoperator.traefik.logging.sapcloud.io utiliza o termo
> 'logging' que apresenta alta similaridade visual e fonetica com a
> marca 'loggi', operando sob uma estrutura de subdominios complexa
> tipica de campanhas de phishing ou infraestrutura de redirecionamento
> nao autorizada, mesmo exibindo um titulo generico de servico interno.

Infraestrutura interna real da SAP Cloud Platform (Traefik = proxy,
"logging" = telemetria de log de software) — nada a ver com a Loggi.

### `blog.cislerfoods.ae` → matched_brand: **loggi** (confiança 0,95)
> O dominio monitorado 'blog.cislerfoods.ae' apresenta alta similaridade
> e tentativa de typosquatting em relacao a marca legitima 'loggi'. O
> conteudo extraido do site aparente ser um servico de SEO gerado por IA
> generico (AutoSEO) usado para mascarar a verdadeira natureza do
> dominio e evitar deteccao automatica, o que e um comportamento tipico
> de infraestruturas de phishing e fraude em dominios cybersquatting.

Domínio contém "foods" — se colidisse com alguma marca monitorada seria
com ifood, não loggi. Nem essa relação é convincente; a suspeita de
SEO/spam pode ser real, mas o `matched_brand` está errado.

### `kouchi.coop-pcsupport.com` → matched_brand: **ifood** (confiança 0,95)
> O dominio 'kouchi.coop-pcsupport.com' utiliza termos associados a
> suporte de computadores e universidades japonesas, mas possui alta
> similaridade com a marca iFood no contexto de analise de ameacas
> (domain squatting ou phishing estruturado), alem de nao pertencer aos
> canais oficiais da marca alvo.

O próprio reasoning do modelo admite a fraqueza da relação ("mas possui
alta similaridade... no contexto de análise de ameaças" é justificativa
circular) — suporte de TI cooperativo japonês, sem relação real com iFood.

## Capturas legítimas, para contraste (mesmo código antigo, mesmo run)

O threshold antigo não é só falso positivo — ele também pega caso real de
typosquat com convicção alta:

### `www.loikliot.com` → matched_brand: **loggi** (confiança 1,00)
> O dominio www.loikliot.com apresenta alta similaridade visual e
> fonetica com a marca legitima 'Loggi', caracterizando um
> typo-squatting. O conteudo extraido exibe padroes incoerentes, termos
> aleatorios e mensagens genericas de pagina em desenvolvimento
> ('Webpage is under development'), o que e tipico de infraestruturas
> preparadas para phishing ou campanhas de fraude contra clientes da
> marca imitada.

### `loggify.padlock.com.co` → matched_brand: **loggi** (confiança 0,95)
> O dominio loggify.padlock.com.co utiliza typosquatting e alta
> similaridade com a marca Loggi combinada com termos genericos de
> login, indicando uma infraestrutura de phishing ou coleta nao
> autorizada de credenciais.

### `bits-of-gold-loggin.pages.dev` → matched_brand: **loggi** (confiança 0,99)
> O dominio suspeito 'bits-of-gold-loggin.pages.dev' utiliza uma
> estrutura de typosquatting/termo composto ('loggin') associada a uma
> pagina de login de criptomoedas, caracteristica tipica de campanhas de
> phishing e roubo de credenciais (credential harvesting).

## O que isso prova

O fix de `DEFAULT_MAX_EDIT_DISTANCE` (2→1, `FINDINGS.md` item 10) não é
cosmético: sob `distance=2`, pelo menos 3 dos 5 casos de colisão acima
(`toledomudlogging.com`, `autoconfig.toledomudlogging.com`,
`interoperator.traefik.logging.sapcloud.io`) têm a MESMA raiz —
"logging" (substantivo comum em inglês) — colidindo com "loggi" (marca).
Isso confirma, com caso real e reasoning completo do Gemini (não só a
contagem agregada), a limitação de "colisão de dicionário" já registrada
como conhecida-e-não-corrigida em `FINDINGS.md` item 10. Comparar esses
mesmos padrões de domínio contra o código com `distance=1` no run oficial
(`obs-2026-08-27`) é o próximo teste natural, quando/se algum desses
padrões reaparecer no CT real.
