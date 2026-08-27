import Link from "next/link";
import type { Investigation } from "@/lib/types";
import { ConfidenceBadge, DomainAgeBadge, InjectionSignalBadge } from "@/components/badges";
import { EvidenceScreenshot } from "@/components/EvidenceScreenshot";
import { DecisionForm } from "@/components/DecisionForm";

export function ReviewCard({ investigation }: { investigation: Investigation }) {
  const evidence = investigation.evidence;
  const screenshot = evidence?.screenshot;

  return (
    <article className="flex flex-col overflow-hidden rounded-2xl border border-zinc-800 bg-zinc-900/50 shadow-lg shadow-black/20">
      {screenshot && evidence ? (
        <EvidenceScreenshot
          screenshot={screenshot}
          formSignal={evidence.form_fields_detected}
          domain={investigation.domain}
        />
      ) : (
        <div className="flex h-40 items-center justify-center bg-zinc-950 text-xs text-zinc-600">
          {evidence?.collection_errors.find((e) => e.step === "screenshot")
            ? "Screenshot indisponível (falha na coleta)"
            : "Sem screenshot"}
        </div>
      )}

      <div className="flex flex-1 flex-col gap-3 p-4">
        <div className="flex items-start justify-between gap-2">
          <div>
            <Link
              href={`/review/${encodeURIComponent(investigation.domain)}`}
              className="font-mono text-sm font-semibold text-zinc-100 hover:text-rose-300 hover:underline"
            >
              {investigation.domain}
            </Link>
            {investigation.matched_brand && (
              <p className="mt-0.5 text-xs text-zinc-500">
                imita <span className="font-medium text-zinc-300">{investigation.matched_brand}</span>
              </p>
            )}
          </div>
          <ConfidenceBadge confidence={investigation.confidence} />
        </div>

        <div className="flex flex-wrap gap-1.5">
          <DomainAgeBadge hours={evidence?.rdap?.domain_age_hours} />
          {evidence?.is_partial && (
            <span className="inline-flex items-center rounded-full bg-zinc-800 px-2.5 py-1 text-xs font-medium text-zinc-500 ring-1 ring-zinc-700">
              evidência parcial
            </span>
          )}
        </div>

        <p className="line-clamp-3 text-xs leading-relaxed text-zinc-400">{investigation.reasoning}</p>

        {(investigation.injection_signals ?? []).length > 0 && (
          <div className="flex flex-wrap gap-1">
            {investigation.injection_signals.map((signal) => (
              <InjectionSignalBadge key={signal} signal={signal} />
            ))}
          </div>
        )}

        <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px] text-zinc-500">
          <div className="flex justify-between border-b border-zinc-800/60 py-1">
            <dt>IP</dt>
            <dd className="font-mono text-zinc-400">{evidence?.hosting?.ip_address ?? "—"}</dd>
          </div>
          <div className="flex justify-between border-b border-zinc-800/60 py-1">
            <dt>ASN</dt>
            <dd className="font-mono text-zinc-400">{evidence?.hosting?.asn_org ?? "—"}</dd>
          </div>
          <div className="flex justify-between border-b border-zinc-800/60 py-1">
            <dt>Registrar</dt>
            <dd className="truncate font-mono text-zinc-400">{evidence?.rdap?.registrar ?? "—"}</dd>
          </div>
          <div className="flex justify-between border-b border-zinc-800/60 py-1">
            <dt>Cert. TLS</dt>
            <dd className="truncate font-mono text-zinc-400">{evidence?.tls_certificate?.issuer ?? "—"}</dd>
          </div>
        </dl>

        <div className="mt-auto pt-1">
          <DecisionForm domain={investigation.domain} />
        </div>
      </div>
    </article>
  );
}
