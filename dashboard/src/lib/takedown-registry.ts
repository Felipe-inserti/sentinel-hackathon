import "server-only";
import Ajv from "ajv";
import { getFirestore, AGENT_REGISTRY_COLLECTION } from "@/lib/gcp";
import type { AgentManifest } from "@/lib/types";

/**
 * Espelha `registry.invoke_agent` (registry.py) em TypeScript -- MESMO
 * papel, mesma coleção Firestore (`agent_registry`), mesmas duas recusas
 * auditáveis (não-ACTIVE / payload fora do schema). Não é um caminho
 * paralelo de invocação: é o dashboard, como qualquer outro consumidor,
 * descobrindo e validando o contrato publicado do `takedown-agent` ANTES
 * de publicar em `takedown-approved` -- em vez de montar esse payload às
 * cegas.
 */
export class AgentInvocationError extends Error {}

const ajv = new Ajv({ strict: false, allowUnionTypes: true });

async function getActiveManifest(agentId: string): Promise<AgentManifest> {
  const snapshot = await getFirestore()
    .collection(AGENT_REGISTRY_COLLECTION)
    .where("agent_id", "==", agentId)
    .where("status", "==", "ACTIVE")
    .get();

  if (snapshot.empty) {
    throw new AgentInvocationError(`Nenhuma versão ACTIVE do agente '${agentId}' encontrada no registry`);
  }

  // Mesma semântica de registry.get_agent: a versão semver mais alta entre
  // as ACTIVE.
  const manifests = snapshot.docs.map((doc) => doc.data() as AgentManifest);
  manifests.sort((a, b) => compareSemver(b.version, a.version));
  return manifests[0];
}

function compareSemver(a: string, b: string): number {
  const pa = a.split(".").map(Number);
  const pb = b.split(".").map(Number);
  for (let i = 0; i < 3; i++) {
    if (pa[i] !== pb[i]) return pa[i] - pb[i];
  }
  return 0;
}

/** Resolve a versão ACTIVE de `agentId` e valida `payload` contra o
 * `input_schema` publicado. Lança `AgentInvocationError` (nunca executa
 * nada) -- quem chama continua responsável por publicar/gravar. */
export async function invokeAgent(agentId: string, payload: unknown): Promise<AgentManifest> {
  const manifest = await getActiveManifest(agentId);

  const validate = ajv.compile(manifest.input_schema);
  if (!validate(payload)) {
    const details = ajv.errorsText(validate.errors, { separator: "; " });
    throw new AgentInvocationError(`Payload inválido para '${manifest.agent_id}@${manifest.version}': ${details}`);
  }

  return manifest;
}
