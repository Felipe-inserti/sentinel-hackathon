import { getFirestore, METRICS_FIRESTORE_COLLECTION } from "@/lib/gcp";
import { sseResponse } from "@/lib/sse";
import { computeReport, computeFunnel, fetchConfirmedMaliciousCount, METRICS_DOCUMENT_ID } from "@/lib/metrics";
import type { PipelineTotals } from "@/lib/types";

export const dynamic = "force-dynamic";

export async function GET() {
  return sseResponse((send, signal) => {
    const docRef = getFirestore().collection(METRICS_FIRESTORE_COLLECTION).doc(METRICS_DOCUMENT_ID);

    const unsubscribe = docRef.onSnapshot(
      (snapshot) => {
        const totals = (snapshot.data() as PipelineTotals | undefined) ?? {};
        // A contagem de confirmados-maliciosos não é um contador OTel (é
        // agregada direto de `investigations`, mesma lógica de
        // metrics_report.py) -- recontamos a cada mudança do doc de
        // métricas, que já não dispara com muita frequência.
        fetchConfirmedMaliciousCount()
          .catch(() => 0)
          .then((confirmedMalicious) => {
            const report = computeReport(totals, confirmedMalicious);
            const funnel = computeFunnel(report);
            send({ report, funnel });
          });
      },
      (error) => send({ error: error.message })
    );

    signal.addEventListener("abort", unsubscribe);
    return unsubscribe;
  });
}
