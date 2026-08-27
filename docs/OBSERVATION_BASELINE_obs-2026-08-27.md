# Linha de base — `obs-2026-08-27` (encerrado deliberadamente, dado preservado)

Este run foi **interrompido de propósito** em 27/08/2026 (~21:52 UTC),
não por falha — a segmentação manual de uma amostra de MALICIOUS (seção
abaixo) revelou que a lógica de classificação atual (prefiltro por
similaridade de string + Gemini com sinais estruturados, sem imagem) gera
**0% de phishing genuíno confirmado contra as marcas monitoradas** numa
amostra de 30. Antes de corrigir essa lógica, este documento congela o
estado do run como ele estava, pra servir de comparação "antes/depois"
contra o próximo run. Nenhum dado de `observation_runs/obs-2026-08-27`
nem de `investigations` foi apagado — só o Scheduler foi pausado e as
execuções ativas canceladas.

Gasto parado, confirmado por execução: `runningCount=0` nos 4 Cloud Run
Jobs, os 4 `google_cloud_scheduler_job` em `PAUSED`.

## Funil completo (saída real de `observation_report.py --run-id obs-2026-08-27 --format markdown`)

- Iniciado em: `2026-08-27 19:17:46.586763+00:00`
- Encerrado (última atualização antes do cancelamento): `2026-08-27 21:52:38.351801+00:00`
- Duração real: ~2h35min
- Tempo médio certificado → dossiê: **6,4s**

| Etapa | Contagem | % do topo | Custo acumulado (USD) |
|---|---|---|---|
| Certificados ingeridos | 631.582 | 100,00% | $0,0000 |
| Sobreviventes do prefiltro | 4.346 | 0,69% | $0,0000 |
| Sobreviventes da triagem Gemma | 4.341 | 0,69% | $0,0000 |
| Investigados pelo Gemini | 4.664 | 0,74% | $3,3322 |
| Confirmados maliciosos | 1.105 | 0,17% | $3,3322 |

### Custo

- Custo real (Gemini): **$3,3322**
- Custo do Gemma (CPU self-hosted): $0,0000
- Custo hipotético SEM a cascata (tudo direto no Gemini): $582,9803
- Economia gerada pela cascata: $578,9687

### Cobertura do certstream/RFC 6962

- Desconexões: 0
- Lacuna total sem cobertura: 0,0s

### Top marcas visadas (funil completo, 3.186+ sobreviventes do prefiltro investigados)

| Marca | Dossiês MALICIOUS | Sobreviventes do prefiltro (todas classificações) | Taxa MALICIOUS |
|---|---|---|---|
| loggi | 2.559 | 2.289 (medido em snapshot intermediário) | ~28% |
| ifood | 705 | 639 | ~20% |
| nubank | 275 | 258 | **~80%** |

(A tabela de sobreviventes/taxa foi medida num snapshot às 21:32 UTC, ~20min antes do encerramento — os totais de MALICIOUS por marca acima já refletem o run completo até 21:52; a taxa % é aproximada por essa defasagem, não recalculada no momento exato do encerramento.)

`nubank` tem a menor fatia de volume mas a maior taxa de confirmação
MALICIOUS — investigado na seção de segmentação abaixo, causa não
determinada (pode ser o mesmo efeito de colisão genérica via "bank" em
vez de "nubank", só que num pool menor).

## Segmentação manual — amostra de 30 de 979 MALICIOUS (seed=42, reproduzível)

Perguntas: cada dossiê é (A) phishing genuíno contra nubank/ifood/loggi,
(B) fraude real mas não-phishing-contra-essas-marcas (apostas, spam,
malware, phishing de outra marca), ou (C) falso positivo por colisão de
string, sem conteúdo malicioso real?

| Categoria | Contagem | % |
|---|---|---|
| **A. Phishing genuíno contra nubank/ifood/loggi** | **0** | **0%** |
| B. Fraude não-phishing (apostas, spam, malware, phishing de outra marca) | 13 | 43,3% |
| C. Falso positivo por colisão de string | 17 | 56,7% |

### Confirmação adicional — busca no total de 979 (não só a amostra)

Domínios MALICIOUS que contêm o nome literal da marca (sinal mais forte
possível de alvo real):

- `"nubank"` no domínio: **0 casos**
- `"ifood"` no domínio: 3 casos — nenhum resiste a inspeção
- `"loggi"` no domínio: 2 casos — ambos `logging.*` (log de software, não a marca)

### 3 exemplos de B

- **`login6-green.pages.dev`** (loggi, conf 1,00) — cassino/apostas indonésio (`HIJAUTOTO`, `Toto Macau`, `AKUN GACOR`); "log" vem de "login" genérico, não da marca.
- **`911jogologin.com`** / **`89pglogin.com`** / **`rrwinlogin.com`** (loggi) — mesmo padrão, apostas online.
- **`rakutenbank-lloydsbank.com.ph`** (nubank, conf 0,95) — phishing bancário real, contra Rakuten Bank e Lloyds Bank, não Nubank.

### 3 exemplos de C

- **`corridorlinklogistics.org`** (loggi) — parking page "Coming Soon", nome de negócio genérico de logística.
- **`trump.blog.cinnemark.com`** (loggi) — domínio à venda (Cinemark, rede de cinema), colisão via "blog".
- **`wizardingbank.direct.quickconnect.to`** (nubank, conf 0,99) — DDNS pessoal Synology, nome-piada de Harry Potter, colisão via "bank".

### Melhores 3 "candidatos" a A no total de 979 — nenhum resiste

- `aifoodcard-pages.pages.dev` (conf 0,99): sem screenshot coletado, reasoning descreve "termos genéricos em chinês".
- `omnifood.omnigestaopro.tech` (conf 0,95): plataforma B2B genérica, inferência por nome.
- `logging.pludoni.de` (conf 0,95): interface real do Graylog (log de servidor).

## O que isto significa

A lógica de classificação atual (prefiltro por similaridade de string +
Gemini com sinais estruturados, sem imagem enviada — ver diagnóstico em
`docs/EVIDENCE_VISION_DIAGNOSIS.md`) produz alto volume de dossiês
MALICIOUS, mas quase nenhum é phishing genuíno contra as 3 marcas
monitoradas. Isso não invalida a arquitetura (a cascata de custo continua
funcionando exatamente como projetada — $3,33 gastos em vez de $582,98
hipotéticos), mas invalida usar este run como demonstração de "detecção
de phishing" sem qualificar a segmentação. Comparar este baseline contra
o próximo run (lógica corrigida) é o objetivo — nenhum dado deste run foi
apagado para permitir essa comparação.
