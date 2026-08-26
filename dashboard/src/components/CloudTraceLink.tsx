/**
 * Link pro Cloud Trace do projeto -- NÃO é um deep-link pro trace exato
 * desta investigação, porque `orchestrator.py`/`evidence_agent.py` hoje
 * não persistem `trace_id` no documento (só o log estruturado, via
 * `telemetry.py::_JsonTraceFormatter`, carrega o trace correlacionado --
 * isso fica no Cloud Logging, não no Firestore). Persistir o `trace_id`
 * no dossiê exigiria mudar o código Python existente do pipeline, e a
 * instrução deste sprint foi parar e perguntar antes disso -- fica como
 * pendência registrada, ver dashboard/README.md.
 *
 * Lê `NEXT_PUBLIC_GCP_PROJECT_ID` (não `GCP_PROJECT_ID` de src/lib/gcp.ts)
 * de propósito: este componente é renderizado dentro de uma página client
 * ([domain]/page.tsx), e `lib/gcp.ts` importa `@google-cloud/firestore`/
 * `pubsub` -- módulos Node.js puros que não podem entrar no bundle do
 * navegador. O project id não é segredo, então expor via NEXT_PUBLIC_ é
 * seguro.
 */
export function CloudTraceLink({ around }: { around: string }) {
  const href = `https://console.cloud.google.com/traces/list?project=${process.env.NEXT_PUBLIC_GCP_PROJECT_ID}`;
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="inline-flex items-center gap-1.5 text-xs font-medium text-cyan-400 hover:text-cyan-300 hover:underline"
      title={`Filtre pelo horário próximo de ${new Date(around).toLocaleString("pt-BR")} -- trace_id exato ainda não é persistido no dossiê`}
    >
      🔗 Ver traces no Cloud Trace ↗
    </a>
  );
}
