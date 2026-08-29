#!/usr/bin/env bash
#
# Sentinel -- serve.sh (Sprint multimodal, alvo de teste local)
#
# Sobe UMA das variantes deste diretorio como `index.html` (raiz do
# servidor) e inicia `python3 -m http.server` local. Necessario porque o
# pipeline real (orchestrator.py::_target_url, evidence_agent.py::_target_url)
# sempre busca `http://{dominio}:{porta}` SEM path -- nunca
# `/malicious.html` etc. -- entao so um arquivo por vez pode ser servido
# na raiz. As variantes ficam em arquivos com nome proprio (nunca
# sobrescritos): `index.html` gerado aqui e so uma COPIA descartavel.
#
# Uso:
#   ./serve.sh [variante] [porta]
#
# Variantes:
#   malicious           (default) -- BancoTeste, formulario de credencial, sem injecao
#   benign                        -- BancoTeste, pagina institucional, SEM formulario (teste de falso positivo)
#   injection-css                 -- malicious.html + injecao (a) texto oculto por CSS
#   injection-comment             -- malicious.html + injecao (b) comentario HTML
#   injection-unicode             -- malicious.html + injecao (c) Unicode Tag Characters
#
# Depois de subir, aponte o Sentinel para ca (ver docs/DEMO_COMMANDS.md):
#   DEMO_INSECURE_HTTP=true
#   DEMO_LOCAL_HTTP_PORT=<porta>
#   echo "127.0.0.1 <dominio-de-teste>" | sudo tee -a /etc/hosts
#
# `<dominio-de-teste>` e QUALQUER nome que voce escolher (ex:
# "bancoteste-fake.sentinel.local") -- o prefilter/orchestrator investigam
# esse nome, o conteudo vem daqui.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VARIANT="${1:-malicious}"
PORT="${2:-8000}"

declare -A FILES=(
  [malicious]="malicious.html"
  [benign]="benign.html"
  [injection-css]="injection-css-hidden.html"
  [injection-comment]="injection-html-comment.html"
  [injection-unicode]="injection-unicode-tags.html"
)

SRC="${FILES[$VARIANT]:-}"
if [[ -z "$SRC" ]]; then
  echo "Variante desconhecida: '$VARIANT'" >&2
  echo "Use uma de: ${!FILES[*]}" >&2
  exit 1
fi

if [[ ! -f "$DIR/$SRC" ]]; then
  echo "Arquivo nao encontrado: $DIR/$SRC" >&2
  exit 1
fi

cp "$DIR/$SRC" "$DIR/index.html"

echo "Servindo '$SRC' (variante: $VARIANT) como index.html"
echo "  URL local:        http://localhost:${PORT}/"
echo "  DEMO_INSECURE_HTTP=true"
echo "  DEMO_LOCAL_HTTP_PORT=${PORT}"
echo
echo "Ctrl+C para parar."
echo

cd "$DIR"
exec python3 -m http.server "$PORT"
