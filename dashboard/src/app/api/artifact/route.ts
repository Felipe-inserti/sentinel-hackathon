import { NextRequest, NextResponse } from "next/server";
import { Storage } from "@google-cloud/storage";
import { GCP_PROJECT_ID } from "@/lib/gcp";

const EVIDENCE_BUCKET =
  process.env.EVIDENCE_GCS_BUCKET || `${GCP_PROJECT_ID}-sentinel-evidence`;

let _storage: Storage | undefined;
function getStorage(): Storage {
  if (!_storage) _storage = new Storage({ projectId: GCP_PROJECT_ID });
  return _storage;
}

/**
 * Proxy autenticado pros artefatos de evidência (screenshot/HTML) em
 * `evidence_agent.EvidenceBundle` -- o navegador nunca recebe uma URI
 * `gs://` nem credencial nenhuma; o browser aponta pra
 * `/api/artifact?uri=gs://...`, este handler valida que a URI pertence ao
 * bucket de evidência configurado (nunca proxeia bucket arbitrário) e
 * streama o objeto usando a Service Account do próprio Cloud Run.
 *
 * HTML nunca é servido inline (`Content-Disposition: attachment` sempre)
 * -- renderizar o HTML sanitizado de uma página de phishing dentro do
 * dashboard reabriria o mesmo risco de rede/tracking que
 * evidence_agent.py tomou cuidado de isolar (Playwright sandboxado,
 * navegação travada no domínio). Só imagem (screenshot) é servida inline.
 */
export async function GET(request: NextRequest) {
  const uri = request.nextUrl.searchParams.get("uri");
  if (!uri || !uri.startsWith(`gs://${EVIDENCE_BUCKET}/`)) {
    return NextResponse.json({ error: "URI de artefato inválida ou fora do bucket de evidência" }, { status: 400 });
  }

  const objectPath = uri.slice(`gs://${EVIDENCE_BUCKET}/`.length);
  const file = getStorage().bucket(EVIDENCE_BUCKET).file(objectPath);

  const [exists] = await file.exists();
  if (!exists) {
    return NextResponse.json({ error: "Artefato não encontrado" }, { status: 404 });
  }

  const [metadata] = await file.getMetadata();
  const contentType = metadata.contentType ?? "application/octet-stream";
  const isImage = contentType.startsWith("image/");

  const [buffer] = await file.download();

  return new NextResponse(new Uint8Array(buffer), {
    headers: {
      "Content-Type": contentType,
      "Content-Disposition": isImage ? "inline" : `attachment; filename="${objectPath.split("/").pop()}"`,
      "Cache-Control": "private, max-age=300",
    },
  });
}
