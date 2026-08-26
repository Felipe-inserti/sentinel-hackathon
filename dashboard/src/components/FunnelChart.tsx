import type { FunnelStep } from "@/lib/metrics";

/** Mesma cascata de 3 níveis de `metrics_report.py::render_funnel`, em
 * barras -- não é um número novo, é o mesmo funil do relatório de
 * terminal. */
export function FunnelChart({ steps }: { steps: FunnelStep[] }) {
  const max = Math.max(...steps.map((s) => s.count), 1);
  return (
    <div className="space-y-3">
      {steps.map((step) => (
        <div key={step.label}>
          <div className="mb-1 flex items-baseline justify-between text-xs">
            <span className="font-medium text-zinc-300">{step.label}</span>
            <span className="font-mono text-zinc-500">
              {step.count.toLocaleString("pt-BR")} · {step.pctOfTop.toFixed(2)}% · ${step.cumulativeCost.toFixed(4)}
            </span>
          </div>
          <div className="h-2.5 w-full overflow-hidden rounded-full bg-zinc-800">
            <div
              className="h-full rounded-full bg-gradient-to-r from-rose-600 to-amber-500"
              style={{ width: `${Math.max((step.count / max) * 100, step.count > 0 ? 1.5 : 0)}%` }}
            />
          </div>
        </div>
      ))}
    </div>
  );
}
