/**
 * Clientes GCP server-only (nunca importar este arquivo de um Client
 * Component). Mesmo padrão de singleton em nível de módulo usado no
 * pipeline Python (`registry.py::db`, `evidence_agent.py::db`) -- um
 * cliente por processo, autenticado via Application Default Credentials
 * (no Cloud Run: a Service Account anexada ao serviço; local: `gcloud auth
 * application-default login`). Nenhuma credencial de arquivo é lida aqui.
 *
 * Nomes de coleção/tópico vêm de env vars com os MESMOS defaults de
 * `config.py`/`.env.example` -- este app lê o que o pipeline já produz,
 * não redefine nomenclatura própria.
 */

import "server-only";
import { Firestore } from "@google-cloud/firestore";
import { PubSub } from "@google-cloud/pubsub";

export const GCP_PROJECT_ID = process.env.GCP_PROJECT_ID ?? "";
export const FIRESTORE_COLLECTION =
  process.env.FIRESTORE_COLLECTION ?? "investigations";
export const AGENT_REGISTRY_COLLECTION =
  process.env.AGENT_REGISTRY_COLLECTION ?? "agent_registry";
export const METRICS_FIRESTORE_COLLECTION =
  process.env.METRICS_FIRESTORE_COLLECTION ?? "metrics";
export const TAKEDOWN_TOPIC_ID =
  process.env.TAKEDOWN_TOPIC_ID ?? "takedown-approved";

// A checagem é dentro das funções (lazy), não no topo do módulo: o
// `next build` importa módulos de Route Handler só pra inspecionar
// exports/config numa fase de "collect page data" que NÃO passa env vars
// de runtime (só as NEXT_PUBLIC_*, que ficam embutidas no bundle) -- uma
// checagem eager aqui derruba o build de Docker mesmo sem nenhum handler
// ser de fato chamado. Falha alto e cedo continua valendo, só que no
// primeiro uso real (runtime), não na importação do módulo -- mesma
// disciplina de config.py (gcp_project_id sem default), adaptada à
// diferença entre build time e runtime do Next.js.
function requireProjectId(): string {
  if (!GCP_PROJECT_ID) {
    throw new Error(
      "GCP_PROJECT_ID não definido. Configure no .env (dev) ou nas env vars do " +
        "Cloud Run (deploy) -- ver dashboard/README.md."
    );
  }
  return GCP_PROJECT_ID;
}

let _firestore: Firestore | undefined;
export function getFirestore(): Firestore {
  if (!_firestore) {
    _firestore = new Firestore({ projectId: requireProjectId() });
  }
  return _firestore;
}

let _pubsub: PubSub | undefined;
export function getPubSub(): PubSub {
  if (!_pubsub) {
    _pubsub = new PubSub({ projectId: requireProjectId() });
  }
  return _pubsub;
}
