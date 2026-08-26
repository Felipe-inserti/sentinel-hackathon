import type { EvidenceBundle } from "@/lib/types";
import { Hash } from "@/components/badges";
import { EvidenceScreenshot } from "@/components/EvidenceScreenshot";

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-xl border border-zinc-800 bg-zinc-900/40 p-4">
      <h3 className="mb-3 text-xs font-semibold uppercase tracking-wide text-zinc-500">{title}</h3>
      {children}
    </section>
  );
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3 border-b border-zinc-800/60 py-1.5 text-xs last:border-0">
      <dt className="text-zinc-500">{label}</dt>
      <dd className="text-right font-mono text-zinc-300">{value ?? "—"}</dd>
    </div>
  );
}

export function EvidencePanel({ evidence, domain }: { evidence: EvidenceBundle; domain: string }) {
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      {evidence.is_partial && (
        <div className="col-span-full rounded-xl border border-amber-800/50 bg-amber-950/20 p-3 text-xs text-amber-300">
          <strong>Bundle parcial.</strong> {evidence.collection_errors.length} etapa(s) de coleta falharam:{" "}
          {evidence.collection_errors.map((e) => e.step).join(", ")}.
        </div>
      )}

      <div className="col-span-full">
        {evidence.screenshot ? (
          <EvidenceScreenshot screenshot={evidence.screenshot} formSignal={evidence.form_fields_detected} domain={domain} />
        ) : (
          <div className="flex h-48 items-center justify-center rounded-xl border border-dashed border-zinc-800 text-xs text-zinc-600">
            Screenshot não coletado
          </div>
        )}
      </div>

      <Section title="RDAP">
        <dl>
          <Row label="Registrar" value={evidence.rdap?.registrar} />
          <Row
            label="Domínio criado em"
            value={evidence.rdap?.domain_created_at ? new Date(evidence.rdap.domain_created_at).toLocaleString("pt-BR") : null}
          />
          <Row
            label="Idade do domínio"
            value={evidence.rdap?.domain_age_hours != null ? `${evidence.rdap.domain_age_hours.toFixed(1)}h` : null}
          />
          <Row label="Contatos de abuse" value={evidence.rdap?.abuse_contacts.join(", ") || null} />
        </dl>
      </Section>

      <Section title="Hospedagem">
        <dl>
          <Row label="IP" value={evidence.hosting?.ip_address} />
          <Row label="ASN" value={evidence.hosting?.asn} />
          <Row label="Organização (ASN)" value={evidence.hosting?.asn_org} />
        </dl>
      </Section>

      <Section title="Certificado TLS">
        <dl>
          <Row label="Emissor" value={evidence.tls_certificate?.issuer} />
          <Row label="Subject" value={evidence.tls_certificate?.subject} />
          <Row
            label="Validade"
            value={
              evidence.tls_certificate?.not_before && evidence.tls_certificate?.not_after
                ? `${new Date(evidence.tls_certificate.not_before).toLocaleDateString("pt-BR")} – ${new Date(
                    evidence.tls_certificate.not_after
                  ).toLocaleDateString("pt-BR")}`
                : null
            }
          />
          <Row label="SANs" value={evidence.tls_certificate?.san.join(", ") || null} />
        </dl>
      </Section>

      <Section title="DNS">
        <dl>
          <Row label="A" value={evidence.dns_records?.a.join(", ") || null} />
          <Row label="AAAA" value={evidence.dns_records?.aaaa.join(", ") || null} />
          <Row label="NS" value={evidence.dns_records?.ns.join(", ") || null} />
          <Row label="MX" value={evidence.dns_records?.mx.join(", ") || null} />
          <Row label="TXT" value={evidence.dns_records?.txt.join(", ") || null} />
        </dl>
      </Section>

      <Section title="HTTP">
        <dl>
          <Row label="Status" value={evidence.http_response?.status_code} />
          <Row label="URL final" value={evidence.http_response?.final_url} />
          <Row label="Redirects" value={evidence.http_response?.redirect_chain.length} />
          <Row label="Server" value={evidence.http_response?.headers?.["Server"] ?? evidence.http_response?.headers?.["server"]} />
        </dl>
      </Section>

      <Section title="Fingerprint de infraestrutura">
        <dl>
          <Row label="Hash do template HTML" value={evidence.infrastructure_fingerprint?.html_template_hash ? <Hash value={evidence.infrastructure_fingerprint.html_template_hash} /> : null} />
          <Row label="Fingerprint combinado" value={evidence.infrastructure_fingerprint?.fingerprint_hash ? <Hash value={evidence.infrastructure_fingerprint.fingerprint_hash} /> : null} />
        </dl>
      </Section>

      <Section title="Chain of custody">
        <dl>
          <Row label="Coletado em (UTC)" value={new Date(evidence.collected_at).toLocaleString("pt-BR")} />
          <Row label="Screenshot SHA-256" value={evidence.screenshot ? <Hash value={evidence.screenshot.sha256} length={16} /> : null} />
          <Row label="HTML SHA-256" value={evidence.html_snapshot ? <Hash value={evidence.html_snapshot.sha256} length={16} /> : null} />
          <Row label="Hash raiz do manifesto" value={<Hash value={evidence.manifest_root_hash} length={20} />} />
        </dl>
        {evidence.html_snapshot && (
          <a
            href={`/api/artifact?uri=${encodeURIComponent(evidence.html_snapshot.gcs_uri)}`}
            className="mt-2 inline-block text-[11px] font-medium text-cyan-400 hover:underline"
          >
            ⬇ baixar HTML sanitizado ({evidence.html_snapshot.size_bytes.toLocaleString("pt-BR")} bytes)
          </a>
        )}
      </Section>
    </div>
  );
}
