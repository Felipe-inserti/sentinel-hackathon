"use server";

import { revalidatePath } from "next/cache";
import { getFirestore, getPubSub, FIRESTORE_COLLECTION, TAKEDOWN_TOPIC_ID } from "@/lib/gcp";
import { getSession } from "@/lib/session";
import { invokeAgent, AgentInvocationError } from "@/lib/takedown-registry";
import type { TakedownChannel } from "@/lib/types";

export interface ActionResult {
  ok: boolean;
  error?: string;
}

const VALID_CHANNELS: TakedownChannel[] = ["registrar_abuse", "hosting_abuse", "brand_protection_vendor"];

/**
 * Aprovar takedown. Grava `approved_by`/`approved_at`/`decision_rationale`
 * no Firestore E publica em `takedown-approved` -- os dois, não um ou
 * outro (regra da trilha: aprovação sem publicação não dispara nada;
 * publicação sem o registro no Firestore não seria auditável).
 *
 * `approved_by` NUNCA vem do formulário -- vem exclusivamente da sessão
 * verificada no servidor (ver src/lib/session.ts). Isso é o que impede um
 * cliente adulterado de forjar "quem aprovou".
 */
export async function approveTakedown(
  domain: string,
  channel: TakedownChannel,
  decisionRationale: string
): Promise<ActionResult> {
  const session = await getSession();
  if (!session) return { ok: false, error: "Sessão expirada -- faça login novamente." };

  if (!VALID_CHANNELS.includes(channel)) {
    return { ok: false, error: "Canal de takedown inválido." };
  }
  const rationale = decisionRationale.trim();
  if (rationale.length < 10) {
    return { ok: false, error: "Justificativa é obrigatória (mínimo 10 caracteres) -- requisito de auditoria." };
  }

  const approvedAt = new Date().toISOString();
  const payload = {
    domain,
    channel,
    approved_by: session.email,
    approved_at: approvedAt,
    decision_rationale: rationale,
  };

  // Descobre + valida o contrato do takedown-agent no registry ANTES de
  // publicar -- mesmo gate que orchestrator.py aplica via
  // registry.invoke_agent (ver src/lib/takedown-registry.ts).
  try {
    await invokeAgent("takedown-agent", payload);
  } catch (err) {
    if (err instanceof AgentInvocationError) {
      return { ok: false, error: `Registry recusou a publicação: ${err.message}` };
    }
    throw err;
  }

  const docRef = getFirestore().collection(FIRESTORE_COLLECTION).doc(domain);
  await docRef.set(
    {
      status: "TAKEDOWN_APPROVED",
      approved_by: session.email,
      approved_at: approvedAt,
      decision_rationale: rationale,
      takedown_channel: channel,
    },
    { merge: true }
  );

  const pubsub = getPubSub();
  const messageId = await pubsub
    .topic(TAKEDOWN_TOPIC_ID)
    .publishMessage({ json: payload });

  revalidatePath("/review");
  revalidatePath(`/review/${domain}`);

  return messageId ? { ok: true } : { ok: false, error: "Falha ao publicar em takedown-approved" };
}

/**
 * Rejeitar. Só grava a decisão (alimenta o feedback loop de um sprint
 * futuro, ainda não implementado) -- nenhuma publicação em Pub/Sub.
 */
export async function rejectInvestigation(domain: string, rejectionReason: string): Promise<ActionResult> {
  const session = await getSession();
  if (!session) return { ok: false, error: "Sessão expirada -- faça login novamente." };

  const reason = rejectionReason.trim();
  if (reason.length < 10) {
    return { ok: false, error: "Motivo é obrigatório (mínimo 10 caracteres) -- requisito de auditoria." };
  }

  const docRef = getFirestore().collection(FIRESTORE_COLLECTION).doc(domain);
  await docRef.set(
    {
      status: "REJECTED",
      rejected_by: session.email,
      rejected_at: new Date().toISOString(),
      rejection_reason: reason,
    },
    { merge: true }
  );

  revalidatePath("/review");
  revalidatePath(`/review/${domain}`);

  return { ok: true };
}
