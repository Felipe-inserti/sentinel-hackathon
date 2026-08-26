import Link from "next/link";
import { getFirestore, FIRESTORE_COLLECTION } from "@/lib/gcp";
import { EmptyState } from "@/components/EmptyState";
import { Hash } from "@/components/badges";
import type { Investigation } from "@/lib/types";

export const dynamic = "force-dynamic";

interface Cluster {
  fingerprintHash: string;
  domains: Investigation[];
}

/** Agrupamento simples por `infrastructure_fingerprint.fingerprint_hash`
 * -- usa o dado que `evidence_agent.py` já calcula, não inventa nada novo.
 * Isto NÃO é o clustering completo do Sprint 7 (que ainda não existe --
 * ver CLAUDE.md/prompt do sprint: "Mapa de campanhas (depende do Sprint
 * 7)"). É um MVP honesto: mesma chave, docs já persistidos, zero
 * infraestrutura nova. Sprint 7 deve trazer, no mínimo, atualização em
 * tempo real e clustering por similaridade parcial (não só hash exato).
 */
async function loadClusters(): Promise<Cluster[]> {
  const snapshot = await getFirestore()
    .collection(FIRESTORE_COLLECTION)
    .where("classification", "==", "MALICIOUS")
    .limit(500)
    .get();

  const byHash = new Map<string, Investigation[]>();
  for (const doc of snapshot.docs) {
    const inv = doc.data() as Investigation;
    const hash = inv.evidence?.infrastructure_fingerprint?.fingerprint_hash;
    if (!hash) continue;
    if (!byHash.has(hash)) byHash.set(hash, []);
    byHash.get(hash)!.push(inv);
  }

  return Array.from(byHash.entries())
    .map(([fingerprintHash, domains]) => ({ fingerprintHash, domains }))
    .filter((c) => c.domains.length >= 2)
    .sort((a, b) => b.domains.length - a.domains.length);
}

export default async function CampaignsPage() {
  const clusters = await loadClusters();

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-zinc-100">Mapa de Campanhas</h1>
        <p className="mt-1 text-sm text-zinc-500">
          Domínios agrupados por fingerprint de infraestrutura idêntico (IP + ASN + registrar + emissor do
          certificado + hash do template HTML).
        </p>
        <p className="mt-2 inline-block rounded-lg border border-zinc-800 bg-zinc-900/50 px-3 py-1.5 text-xs text-zinc-500">
          MVP com o dado que já existe hoje (hash exato). Clustering por similaridade parcial e atualização em
          tempo real são o Sprint 7 -- ainda não implementado.
        </p>
      </div>

      {clusters.length === 0 ? (
        <EmptyState
          icon="🕸️"
          title="Nenhuma campanha detectada ainda"
          description="Precisa de pelo menos dois domínios MALICIOUS com evidência coletada compartilhando o mesmo fingerprint de infraestrutura."
        />
      ) : (
        <div className="space-y-4">
          {clusters.map((cluster) => (
            <div key={cluster.fingerprintHash} className="rounded-xl border border-zinc-800 bg-zinc-900/40 p-4">
              <div className="mb-3 flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span className="rounded-full bg-rose-500/15 px-2.5 py-1 text-xs font-semibold text-rose-300 ring-1 ring-rose-500/30">
                    {cluster.domains.length} domínios
                  </span>
                  <Hash value={cluster.fingerprintHash} length={16} />
                </div>
              </div>
              <ul className="grid grid-cols-1 gap-1.5 sm:grid-cols-2 lg:grid-cols-3">
                {cluster.domains.map((d) => (
                  <li key={d.domain}>
                    <Link
                      href={`/review/${encodeURIComponent(d.domain)}`}
                      className="flex items-center justify-between rounded-lg bg-zinc-950/60 px-3 py-2 text-xs font-mono text-zinc-300 transition hover:bg-zinc-800 hover:text-rose-300"
                    >
                      {d.domain}
                      <span className="text-zinc-600">{d.status ?? "—"}</span>
                    </Link>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
