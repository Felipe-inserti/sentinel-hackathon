import { getFirestore, FIRESTORE_COLLECTION } from "@/lib/gcp";
import { sseResponse } from "@/lib/sse";
import type { Investigation } from "@/lib/types";

export const dynamic = "force-dynamic";

/** Um único dossiê em tempo real -- usado pela tela de detalhe, pra
 * refletir na hora se outra aba/revisor aprovar/rejeitar enquanto esta
 * página está aberta (evita "aprovação dupla"). */
export async function GET(
  _request: Request,
  { params }: { params: Promise<{ domain: string }> }
) {
  const { domain } = await params;

  return sseResponse((send, signal) => {
    const docRef = getFirestore().collection(FIRESTORE_COLLECTION).doc(domain);

    const unsubscribe = docRef.onSnapshot(
      (snapshot) => {
        send({ item: snapshot.exists ? (snapshot.data() as Investigation) : null });
      },
      (error) => {
        send({ error: error.message });
      }
    );

    signal.addEventListener("abort", unsubscribe);
    return unsubscribe;
  });
}
