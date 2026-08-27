"use client";

import { use } from "react";
import Link from "next/link";
import { useEventSource } from "@/lib/useEventSource";
import { ConfidenceBadge, DomainAgeBadge, InjectionSignalBadge, StatusBadge } from "@/components/badges";
import { EvidencePanel } from "@/components/EvidencePanel";
import { Timeline, buildTimeline } from "@/components/Timeline";
import { CloudTraceLink } from "@/components/CloudTraceLink";
import { DecisionForm } from "@/components/DecisionForm";
import { EmptyState } from "@/components/EmptyState";
import type { Investigation } from "@/lib/types";

interface DetailEvent {
  item?: Investigation | null;
  error?: string;
}

export default function InvestigationDetailPage({ params }: { params: Promise<{ domain: string }> }) {
  const { domain } = use(params);
  const { data } = useEventSource<DetailEvent>(`/api/stream/investigation/${encodeURIComponent(domain)}`);

  if (data?.item === null) {
    return (
      <EmptyState
        icon="🔎"
        title="Dossiê não encontrado"
        description={`Nenhuma investigação para "${domain}" no Firestore.`}
      />
    );
  }

  if (!data?.item) {
    return <div className="h-96 animate-pulse rounded-2xl border border-zinc-800 bg-zinc-900/40" />;
  }

  const inv = data.item;

  return (
    <div className="space-y-6">
      <div>
        <Link href="/review" className="text-xs text-zinc-500 hover:text-zinc-300">
          ← voltar pra fila
        </Link>
        <div className="mt-2 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="font-mono text-2xl font-semibold text-zinc-100">{inv.domain}</h1>
            {inv.matched_brand && (
              <p className="text-sm text-zinc-500">
                imita <span className="font-medium text-zinc-300">{inv.matched_brand}</span>
              </p>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge status={inv.status} />
            <ConfidenceBadge confidence={inv.confidence} />
            {inv.evidence?.rdap?.domain_age_hours != null && (
              <DomainAgeBadge hours={inv.evidence.rdap.domain_age_hours} />
            )}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <div className="space-y-6 lg:col-span-2">
          <section className="rounded-xl border border-zinc-800 bg-zinc-900/40 p-4">
            <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">
              Reasoning do modelo ({inv.model})
            </h2>
            <p className="text-sm leading-relaxed text-zinc-300">{inv.reasoning}</p>
            {(inv.injection_signals ?? []).length > 0 && (
              <div className="mt-3 flex flex-wrap gap-1.5">
                {inv.injection_signals.map((s) => (
                  <InjectionSignalBadge key={s} signal={s} />
                ))}
              </div>
            )}
            {inv.requires_human_review && (
              <p className="mt-3 rounded-lg border border-violet-800/50 bg-violet-950/30 p-2 text-xs text-violet-300">
                Sinal de injeção detectado com classificação SAFE do modelo -- revisão humana obrigatória
                independente da decisão do LLM.
              </p>
            )}
          </section>

          {inv.evidence && <EvidencePanel evidence={inv.evidence} domain={inv.domain} />}
        </div>

        <div className="space-y-6">
          <section className="rounded-xl border border-zinc-800 bg-zinc-900/40 p-4">
            <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-zinc-500">Timeline</h2>
            <Timeline events={buildTimeline(inv)} />
            <div className="mt-4 border-t border-zinc-800 pt-3">
              <CloudTraceLink around={inv.investigated_at} />
            </div>
          </section>

          <section className="rounded-xl border border-zinc-800 bg-zinc-900/40 p-4">
            <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-zinc-500">Decisão</h2>
            {inv.status === "TAKEDOWN_APPROVED" ? (
              <div className="space-y-1 text-xs text-zinc-400">
                <p>
                  Aprovado por <span className="text-zinc-200">{inv.approved_by}</span>
                </p>
                <p>canal: {inv.takedown_channel}</p>
                <p className="italic text-zinc-500">&ldquo;{inv.decision_rationale}&rdquo;</p>
              </div>
            ) : inv.status === "REJECTED" ? (
              <div className="space-y-1 text-xs text-zinc-400">
                <p>
                  Rejeitado por <span className="text-zinc-200">{inv.rejected_by}</span>
                </p>
                <p className="italic text-zinc-500">&ldquo;{inv.rejection_reason}&rdquo;</p>
              </div>
            ) : inv.status === "PENDING_HUMAN_REVIEW" ? (
              <DecisionForm domain={inv.domain} />
            ) : (
              <p className="text-xs text-zinc-500">Evidência ainda não coletada -- aguardando evidence-collector.</p>
            )}
          </section>

          <section className="rounded-xl border border-zinc-800 bg-zinc-900/40 p-4">
            <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">Agentes</h2>
            <dl className="space-y-1 text-xs text-zinc-500">
              <div className="flex justify-between">
                <dt>investigação</dt>
                <dd className="font-mono text-zinc-300">
                  {inv.agent_id}@{inv.agent_version}
                </dd>
              </div>
              {inv.evidence_agent_id && (
                <div className="flex justify-between">
                  <dt>evidência</dt>
                  <dd className="font-mono text-zinc-300">
                    {inv.evidence_agent_id}@{inv.evidence_agent_version}
                  </dd>
                </div>
              )}
            </dl>
          </section>
        </div>
      </div>
    </div>
  );
}
