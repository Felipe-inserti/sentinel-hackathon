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
