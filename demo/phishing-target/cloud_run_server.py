"""Sentinel -- servidor HTTP minimo do alvo publico de demo (Sprint 2,
Stage D). Serve os arquivos de demo/phishing-target/ -- SO os 5 aprovados
explicitamente, NUNCA malicious_nofooter.html (nem essa linha de COPY
existe em Dockerfile.demo-target -- o arquivo simplesmente nao esta
presente na imagem, e o allowlist abaixo e uma segunda camada de defesa,
mesmo que alguem um dia adicione o COPY por engano).

Roda como Cloud Run Service -- hostname publico real
(https://sentinel-demo-target-hash.a.run.app), SEM path necessario. Isso
existe porque o bucket GCS (infra/demo_target_bucket.tf, Stage D inicial)
NAO funciona para este proposito -- `website{ main_page_suffix }` do GCS
so tem efeito atras de um Application Load Balancer com dominio proprio
verificado, que este projeto nao tem (ver FINDINGS.md item 17). Um Cloud
Run Service da um hostname puro de verdade sem precisar de dominio
proprio -- mesmo mecanismo que `sentinel-agent-gateway` ja usa neste
projeto.

GET /                -> conteudo do arquivo indicado por SERVE_AS_ROOT
                         (env var, default "malicious.html") -- trocavel
                         SEM rebuild de imagem, via
                         `gcloud run services update sentinel-demo-target
                         --update-env-vars=SERVE_AS_ROOT=<arquivo>.html`
                         (redeploy rapido, mesma imagem, mesmo digest).
GET /<arquivo>.html  -> o arquivo especifico, direto -- link inspecionavel
                        pra qualquer um dos 5, independente do que estiver
                        em SERVE_AS_ROOT no momento.
Qualquer outro path/arquivo fora do allowlist -> 404.

Stdlib puro (`http.server`) -- nenhuma dependencia nova."""

from __future__ import annotations

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEMO_DIR = os.path.dirname(os.path.abspath(__file__))

# Allowlist explicito -- segunda camada de defesa alem do Dockerfile so
# copiar estes arquivos. `malicious_nofooter.html` deliberadamente FORA
# desta lista, mesmo que um dia acabe presente no disco da imagem por
# engano -- nunca seria servido de qualquer forma.
ALLOWED_FILES = frozenset(
    {
        "malicious.html",
        "benign.html",
        "injection-css-hidden.html",
        "injection-html-comment.html",
        "injection-unicode-tags.html",
        "injection-css-generated.html",
    }
)

SERVE_AS_ROOT = os.environ.get("SERVE_AS_ROOT", "malicious.html")
if SERVE_AS_ROOT not in ALLOWED_FILES:
    raise SystemExit(f"SERVE_AS_ROOT={SERVE_AS_ROOT!r} fora do allowlist -- recusando subir.")


class DemoTargetHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - assinatura exigida pela stdlib
        requested = self.path.lstrip("/") or SERVE_AS_ROOT
        if requested not in ALLOWED_FILES:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"not found")
            return

        with open(os.path.join(DEMO_DIR, requested), "rb") as f:
            body = f.read()

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Cloud Run ja captura stdout/stderr do container como log
        # estruturado -- o log default do BaseHTTPRequestHandler (linha
        # crua por request) nao agrega nada aqui, so ruido.
        pass


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), DemoTargetHandler)
    print(f"Servindo alvo de demo em 0.0.0.0:{port} (SERVE_AS_ROOT={SERVE_AS_ROOT})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
