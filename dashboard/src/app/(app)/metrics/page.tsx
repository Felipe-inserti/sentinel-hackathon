"use client";

import { useEventSource } from "@/lib/useEventSource";
import { StatTile } from "@/components/StatTile";
import { FunnelChart } from "@/components/FunnelChart";
import type { TokenEconomyReport, FunnelStep } from "@/lib/metrics";

interface MetricsEvent {
  report?: TokenEconomyReport;
  funnel?: FunnelStep[];
  error?: string;
}

function usd(n: number, digits = 4) {
  return `$${n.toFixed(digits)}`;
}

export default function MetricsPage() {
  const { data } = useEventSource<MetricsEvent>("/api/stream/metrics");
  const r = data?.report;
  const funnel = data?.funnel;

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-xl font-semibold text-zinc-100">Token Economy</h1>
        <p className="mt-1 text-sm text-zinc-500">
          A tese do projeto: quase todo domínio é descartado por matemática pura, custo zero, antes de qualquer
          chamada de LLM.
        </p>
      </div>

      {data?.error && (
        <div className="rounded-lg border border-rose-800/50 bg-rose-950/30 p-3 text-sm text-rose-300">
          Erro ao carregar métricas: {data.error}
        </div>
      )}

      {!r ? (
        <div className="h-64 animate-pulse rounded-2xl border border-zinc-800 bg-zinc-900/40" />
      ) : r.ingested === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-2xl border border-dashed border-zinc-800 bg-zinc-900/30 px-6 py-20 text-center">
          <div className="mb-4 text-4xl">📡</div>
          <h2 className="text-base font-semibold text-zinc-200">Nenhuma métrica ainda</h2>
          <p className="mt-2 max-w-sm text-sm text-zinc-500">
            Rode <code className="font-mono text-zinc-400">ct_listener.py</code> e{" "}
            <code className="font-mono text-zinc-400">orchestrator.py</code> para começar a alimentar{" "}
            <code className="font-mono text-zinc-400">metrics/pipeline_totals</code>.
          </p>
        </div>
      ) : (
        <>
          {/* O número grande do pitch */}
          <div className="rounded-2xl border border-emerald-800/40 bg-gradient-to-br from-emerald-950/40 to-zinc-900/40 p-8 text-center">
            <p className="text-sm font-medium uppercase tracking-widest text-emerald-500">Economia total gerada</p>
            <p className="mt-2 font-mono text-6xl font-bold text-emerald-400 sm:text-7xl">
              {usd(r.totalSaved, 2)}
            </p>
            <p className="mt-3 text-sm text-zinc-500">
              {r.totalReductionPct.toFixed(1)}% de redução vs. enviar tudo direto pro Gemini (
              {usd(r.hypotheticalCostNoPrefilter, 2)} hipotético)
            </p>
            <div className="mx-auto mt-4 flex max-w-md justify-center gap-6 text-xs text-zinc-500">
              <span>prefiltro: {usd(r.costSavedByPrefilter, 2)}</span>
              <span>Gemma: {usd(r.costSavedByGemma, 2)}</span>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6">
            <StatTile label="Certificados ingeridos" value={r.ingested.toLocaleString("pt-BR")} />
            <StatTile
              label="Descartados (prefiltro)"
              value={r.discarded.toLocaleString("pt-BR")}
              sub={`${r.discardRate.toFixed(2)}%`}
              tone="amber"
            />
            <StatTile label="Invocações LLM" value={r.llmInvocations.toLocaleString("pt-BR")} tone="rose" />
            <StatTile
              label="Cache hits"
              value={r.cacheHits.toLocaleString("pt-BR")}
              sub={`${r.cacheHitRate.toFixed(1)}% das investigações`}
              tone="emerald"
            />
            <StatTile label="Tokens consumidos" value={r.tokensConsumed.toLocaleString("pt-BR")} />
            <StatTile label="Custo real (Gemini)" value={usd(r.costReal)} tone="rose" />
          </div>

          <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            <StatTile label="Triagem Gemma" value={r.gemmaTriageTotal.toLocaleString("pt-BR")} />
            <StatTile
              label="Descartados (Gemma)"
              value={r.gemmaDiscarded.toLocaleString("pt-BR")}
              sub={`${r.gemmaDiscardRate.toFixed(2)}%`}
            />
            <StatTile label="Evidências coletadas" value={r.evidenceBundlesCollected.toLocaleString("pt-BR")} />
            <StatTile
              label="Evidências parciais"
              value={r.evidenceBundlesPartial.toLocaleString("pt-BR")}
              tone={r.evidenceBundlesPartial > 0 ? "amber" : "zinc"}
            />
          </div>

          <section className="rounded-xl border border-zinc-800 bg-zinc-900/40 p-5">
            <h2 className="mb-4 text-xs font-semibold uppercase tracking-wide text-zinc-500">
              Funil de custo -- prefiltro → Gemma → Gemini
            </h2>
            {funnel && <FunnelChart steps={funnel} />}
          </section>
        </>
      )}
    </div>
  );
}
