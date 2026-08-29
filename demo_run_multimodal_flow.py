"""Sentinel -- demo PONTA A PONTA local do Sprint multimodal.

UM comando, chamando as MESMAS funcoes de producao (nunca reimplementadas):

  pagina local (demo/phishing-target/serve.sh)
    -> plane2_agents.orchestrator.classify_domain_with_gemini (Gemini
       REAL, multimodal -- texto + screenshot na MESMA chamada)
    -> [se MALICIOUS] evidence_agent.collect_evidence (screenshot/DNS/
       RDAP/TLS reais, upload no GCS real)
    -> aprovacao humana -- REAL (via dashboard) por padrao; SIMULADA so
       com --simulate-approval, gravando os MESMOS campos que
       dashboard/.../review/actions.ts::approveTakedown grava apos um
       clique de verdade (approved_by/approved_at/decision_rationale/
       takedown_channel) -- documentado como simulacao, nunca escondido
    -> takedown_agent.process_takedown_approval (envia o e-mail de demo
       REAL via SMTP se o dominio estiver em DEMO_LIVE_SEND_ALLOWLIST e
       DRY_RUN=false; senao so relata o dry-run, mesmo comportamento de
       producao)

Uso:
    # terminal 1
    ./demo/phishing-target/serve.sh malicious 8000

    # terminal 2
    python demo_run_multimodal_flow.py banco-teste-fake.sentinel.local \\
        --brand bancoteste --category registrar_abuse --simulate-approval

Pre-requisitos (ver docs/DEMO_COMMANDS.md):
  - .env com GCP_PROJECT_ID real + Vertex AI habilitado
    (aiplatform.googleapis.com) -- sem isso, a chamada ao Gemini falha.
  - DEMO_INSECURE_HTTP=true / DEMO_LOCAL_HTTP_PORT=8000
  - <dominio> resolvendo para 127.0.0.1 (echo "127.0.0.1 <dominio>" | sudo tee -a /etc/hosts)
  - Para o e-mail real: DEMO_SMTP_USERNAME/DEMO_SMTP_PASSWORD +
    DEMO_LIVE_SEND_ALLOWLIST={"<dominio>": "seu-email@..."} no .env,
    DRY_RUN=false.

Sem --simulate-approval, o script para depois de mostrar o veredito e
imprime o que falta para aprovar de verdade via dashboard -- nunca envia
nada sem aprovacao (mesma regra do CLAUDE.md, reforcada aqui de proposito:
um script de demo nao e desculpa pra pular a regra #4)."""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone


def _green(s: str) -> str:
    return f"\033[1;32m{s}\033[0m"


def _red(s: str) -> str:
    return f"\033[1;31m{s}\033[0m"


def _dim(s: str) -> str:
    return f"\033[2m{s}\033[0m"


def _bold(s: str) -> str:
    return f"\033[1m{s}\033[0m"


def _step(n: int, total: int, label: str) -> None:
    print(f"\n{_bold(f'[{n}/{total}]')} {label}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("domain", help="dominio de teste (deve resolver para 127.0.0.1, ver docs/DEMO_COMMANDS.md)")
    parser.add_argument("--brand", default="bancoteste", help="marca correspondida (default: bancoteste)")
    parser.add_argument(
        "--category",
        default="registrar_abuse",
        choices=["registrar_abuse", "hosting_abuse", "brand_protection_vendor"],
        help="categoria de takedown para a aprovacao simulada (default: registrar_abuse)",
    )
    parser.add_argument(
        "--simulate-approval",
        action="store_true",
        help="grava uma aprovacao SIMULADA no Firestore (mesmos campos do dashboard) e roda o takedown de demo. Sem esta flag, o script para no veredito.",
    )
    parser.add_argument(
        "--skip-evidence",
        action="store_true",
        help="pula evidence_agent.collect_evidence mesmo se MALICIOUS (GCS bucket pode nao existir ainda)",
    )
    args = parser.parse_args()

    total_steps = 5 if args.simulate_approval else 3

    print(_bold("SENTINEL -- DEMO PONTA A PONTA (multimodal)"))
    print(_dim(f"  dominio: {args.domain}  marca: {args.brand}  categoria: {args.category}"))

    # --- [1] classificacao multimodal (Gemini real) -------------------------
    _step(1, total_steps, "Classificando com Gemini multimodal (texto + screenshot, mesma chamada)")
    import plane2_agents.orchestrator as orchestrator

    (
        result,
        usage,
        sanitized,
        cost_usd,
        memory_usage,
    ) = await orchestrator.classify_domain_with_gemini(args.domain, args.brand, None)

    color = _red if result.classification == "MALICIOUS" else _green
    print(f"  Veredito: {color(result.classification)} (confianca {result.confidence:.2f})")
    print(f"  Reasoning: {result.reasoning}")
    print(f"  Analise visual disponivel nesta chamada: {result.visual_analysis_available}")
    if result.visual_analysis_available:
        print(f"    brand_impersonated:      {result.brand_impersonated}")
        print(f"    visual_brand_match:      {result.visual_brand_match}")
        print(f"    credential_form_present: {result.credential_form_present}")
        print(f"    visual_anomalies:        {result.visual_anomalies}")
        print(f"    text_in_image_summary:   {result.text_in_image_summary}")
    print(
        f"  Custo: input={usage.input_tokens}tok (texto={usage.input_text_tokens}, "
        f"imagem={usage.input_image_tokens}) output={usage.output_tokens}tok -- ${cost_usd:.6f}"
    )
    if sanitized.injection_patterns_found:
        print(_red(f"  Sinais de injecao no texto: {sanitized.injection_patterns_found}"))

    if result.classification != "MALICIOUS":
        print(_dim("\n  Dominio nao classificado como MALICIOUS -- nada mais a fazer nesta demo."))
        return 0

    # --- [2] evidencia (real, se pedida) -------------------------------------
    if not args.skip_evidence:
        _step(2, total_steps, "Coletando dossie de evidencia (evidence_agent.collect_evidence, real)")
        try:
            import evidence_agent

            bundle = await evidence_agent.collect_evidence(args.domain)
            print(f"  is_partial={bundle.is_partial}  screenshot={'OK' if bundle.screenshot else 'ausente'}")
            if bundle.screenshot:
                print(f"    sha256={bundle.screenshot.sha256}")
        except Exception as exc:  # bucket pode nao existir ainda -- nao derruba a demo
            print(_red(f"  Falha ao coletar evidencia (NAO VERIFICADO): {exc.__class__.__name__}: {exc}"))
            print(_dim("  Siga mesmo assim -- a aprovacao/e-mail abaixo nao dependem deste passo."))
    else:
        print(_dim("\n  --skip-evidence: pulando evidence_agent.collect_evidence."))

    if not args.simulate_approval:
        _step(3, total_steps, "Aprovacao humana -- REAL, via dashboard")
        print(_dim("  Sem --simulate-approval: aprove de verdade no dashboard e rode de novo"))
        print(_dim("  com --simulate-approval para dispensar o e-mail de demo, OU deixe o"))
        print(_dim("  takedown-agent real consumir a aprovacao publicada em Pub/Sub."))
        return 0

    # --- [3] aprovacao SIMULADA (mesmos campos do dashboard real) -----------
    _step(3, total_steps, "Gravando aprovacao SIMULADA no Firestore (mesmos campos do dashboard real)")
    from config import settings
    from google.cloud import firestore

    db = firestore.Client()
    approved_at = datetime.now(timezone.utc).isoformat()
    db.collection(settings.firestore_collection).document(args.domain).set(
        {
            "status": "TAKEDOWN_APPROVED",
            "approved_by": "demo-operator@local (SIMULADO -- nao um clique real no dashboard)",
            "approved_at": approved_at,
            "decision_rationale": (
                f"[DEMO] classificado MALICIOUS com confianca {result.confidence:.2f} -- "
                f"identidade visual de marca ({result.brand_impersonated}) + "
                f"formulario de credencial detectado, aprovacao simulada por demo_run_multimodal_flow.py"
            ),
            "takedown_channel": args.category,
        },
        merge=True,
    )
    print(f"  investigations/{args.domain} atualizado (status=TAKEDOWN_APPROVED, SIMULADO).")

    # --- [4] takedown real (respeita DRY_RUN/allowlist, sempre) -------------
    _step(4, total_steps, "Rodando takedown_agent.process_takedown_approval (real)")
    import registry
    import takedown_agent as ta

    manifest = registry.AgentManifest(
        agent_id="takedown-agent",
        version="1.0.0",
        owner_team="sentinel-response",
        description="demo local",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        tools_allowed=[],
        required_permissions=[],
        sla_seconds=5.0,
        status=registry.AgentStatus.ACTIVE,
        created_at=datetime.now(timezone.utc),
    )
    output = await ta.process_takedown_approval(args.domain, manifest)
    print(f"  sent={output.sent}  dry_run={output.dry_run}")
    if output.dry_run:
        print(_dim("  DRY_RUN=true -- nenhum e-mail real enviado (padrao de seguranca)."))
    elif output.sent:
        print(_green(f"  E-mail de demo enviado -- confira {settings.demo_live_send_allowlist.get(args.domain)}."))
    else:
        print(_red("  Nao enviado -- dominio provavelmente fora da DEMO_LIVE_SEND_ALLOWLIST (ver log acima)."))

    _step(5, total_steps, "Fim.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
