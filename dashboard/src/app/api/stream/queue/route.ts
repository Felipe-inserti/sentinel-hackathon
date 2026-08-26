import { getFirestore, FIRESTORE_COLLECTION } from "@/lib/gcp";
import { sseResponse } from "@/lib/sse";
import type { Investigation } from "@/lib/types";

export const dynamic = "force-dynamic";

/** Fila de revisão em tempo real: MALICIOUS + evidência já coletada
 * (`status == PENDING_HUMAN_REVIEW`), ordenado por confiança decrescente
 * -- exatamente o filtro pedido no sprint. Sai da fila sozinho assim que
 * `review/actions.ts` muda o `status` pra TAKEDOWN_APPROVED/REJECTED,
 * porque o snapshot inteiro é reconsultado a cada mudança (onSnapshot já
 * cuida disso, não precisamos remover manualmente). */
export async function GET() {
  return sseResponse((send, signal) => {
    const query = getFirestore()
      .collection(FIRESTORE_COLLECTION)
      .where("classification", "==", "MALICIOUS")
      .where("status", "==", "PENDING_HUMAN_REVIEW")
      .orderBy("confidence", "desc");

    const unsubscribe = query.onSnapshot(
      (snapshot) => {
        const items = snapshot.docs.map((doc) => doc.data() as Investigation);
        send({ items });
      },
      (error) => {
        send({ error: error.message });
      }
    );

    signal.addEventListener("abort", unsubscribe);
    return unsubscribe;
  });
}
