import "server-only";
import { getFirestore, FIRESTORE_COLLECTION, METRICS_FIRESTORE_COLLECTION } from "@/lib/gcp";
import type { PipelineTotals } from "@/lib/types";

export const METRICS_DOCUMENT_ID = "pipeline_totals";

/**
 * Porta DIRETA de `metrics_report.py::compute_report`/`compute_funnel` --
 * mesmos nomes de campo, mesmas fórmulas, mesma fonte (documento
 * `metrics/pipeline_totals`). Não é uma métrica nova: é o painel visual do
 * mesmo número que já sai no relatório de terminal. Qualquer mudança de
 * fórmula deve ser feita nos dois lugares (ou melhor: só no Python, e
 * portada de novo aqui -- ver docstring de metrics_report.py pra contexto
 * de cada campo).
 */
export interface TokenEconomyReport {
  ingested: number;
  discarded: number;
  discardRate: number;
  llmInvocations: number;
  cacheHits: number;
  totalInvestigations: number;
  cacheHitRate: number;
  tokensConsumed: number;
  costReal: number;
  avgCostPerLlmCall: number;
  avgCostPerInvestigation: number;
  hypotheticalCostNoPrefilter: number;
  costSavedByPrefilter: number;
  gemmaTriageTotal: number;
  gemmaDiscarded: number;
  gemmaEscalated: number;
  gemmaFallback: number;
  gemmaCost: number;
  gemmaDiscardRate: number;
  costSavedByGemma: number;
  evidenceBundlesCollected: number;
  evidenceBundlesPartial: number;
  confirmedMalicious: number;
  totalSaved: number;
  totalReductionPct: number;
}

export interface FunnelStep {
  label: string;
  count: number;
  pctOfTop: number;
  cumulativeCost: number;
}

export function computeReport(totals: PipelineTotals, confirmedMalicious: number): TokenEconomyReport {
  const ingested = totals.certificates_ingested_total ?? 0;
  const discarded = totals.certificates_discarded_by_prefilter_total ?? 0;
  const llmInvocations = totals.llm_invocations_total ?? 0;
  const cacheHits = totals.cache_hits_total ?? 0;
  const tokensConsumed = totals.tokens_consumed_total ?? 0;
  const costReal = totals.estimated_cost_usd_total ?? 0;

  const gemmaTriageTotal = totals.gemma_triage_total ?? 0;
  const gemmaDiscarded = totals.gemma_discarded_total ?? 0;
  const gemmaEscalated = totals.gemma_escalated_total ?? 0;
  const gemmaFallback = totals.gemma_fallback_total ?? 0;
  const gemmaCost = totals.gemma_triage_cost_usd_total ?? 0;

  const discardRate = ingested ? (discarded / ingested) * 100 : 0;
  const totalInvestigations = llmInvocations + cacheHits;
  const cacheHitRate = totalInvestigations ? (cacheHits / totalInvestigations) * 100 : 0;

  const avgCostPerLlmCall = llmInvocations ? costReal / llmInvocations : 0;
  const avgCostPerInvestigation = totalInvestigations ? costReal / totalInvestigations : 0;
  const gemmaDiscardRate = gemmaTriageTotal ? (gemmaDiscarded / gemmaTriageTotal) * 100 : 0;

  const hypotheticalCostNoPrefilter = ingested * avgCostPerLlmCall;
  const costSavedByPrefilter = discarded * avgCostPerLlmCall;
  const costSavedByGemma = gemmaDiscarded * avgCostPerLlmCall;
  const totalSaved = costSavedByPrefilter + costSavedByGemma;
  const totalReductionPct = hypotheticalCostNoPrefilter > 0 ? (totalSaved / hypotheticalCostNoPrefilter) * 100 : 0;

  return {
    ingested,
    discarded,
    discardRate,
    llmInvocations,
    cacheHits,
    totalInvestigations,
    cacheHitRate,
    tokensConsumed,
    costReal,
    avgCostPerLlmCall,
    avgCostPerInvestigation,
    hypotheticalCostNoPrefilter,
    costSavedByPrefilter,
    gemmaTriageTotal,
    gemmaDiscarded,
    gemmaEscalated,
    gemmaFallback,
    gemmaCost,
    gemmaDiscardRate,
    costSavedByGemma,
    evidenceBundlesCollected: totals.evidence_bundles_collected_total ?? 0,
    evidenceBundlesPartial: totals.evidence_bundles_partial_total ?? 0,
    confirmedMalicious,
    totalSaved,
    totalReductionPct,
  };
}

export function computeFunnel(r: TokenEconomyReport): FunnelStep[] {
  const prefilterSurvivors = r.ingested - r.discarded;
  const gemmaSurvivors = r.gemmaTriageTotal - r.gemmaDiscarded;
  const geminiInvestigated = r.totalInvestigations;

  const costAfterPrefilter = 0; // matemática pura, custo zero
  const costAfterGemma = r.gemmaCost;
  const costAfterGemini = r.gemmaCost + r.costReal;

  const steps: [string, number, number][] = [
    ["Certificados ingeridos", r.ingested, 0],
    ["Sobreviventes do prefiltro", prefilterSurvivors, costAfterPrefilter],
    ["Sobreviventes da triagem Gemma", gemmaSurvivors, costAfterGemma],
    ["Investigados pelo Gemini", geminiInvestigated, costAfterGemini],
    ["Confirmados maliciosos", r.confirmedMalicious, costAfterGemini],
  ];

  const base = r.ingested || 1;
  return steps.map(([label, count, cumulativeCost]) => ({
    label,
    count,
    pctOfTop: (count / base) * 100,
    cumulativeCost,
  }));
}

/** Conta `investigations` com `classification == "MALICIOUS"` -- mesmo
 * papel de `metrics_report.py::fetch_confirmed_malicious_count`. */
export async function fetchConfirmedMaliciousCount(): Promise<number> {
  const snapshot = await getFirestore()
    .collection(FIRESTORE_COLLECTION)
    .where("classification", "==", "MALICIOUS")
    .count()
    .get();
  return snapshot.data().count;
}

export async function fetchPipelineTotals(): Promise<PipelineTotals> {
  const doc = await getFirestore()
    .collection(METRICS_FIRESTORE_COLLECTION)
    .doc(METRICS_DOCUMENT_ID)
    .get();
  return (doc.data() as PipelineTotals | undefined) ?? {};
}
