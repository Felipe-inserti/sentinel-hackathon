/**
 * Tipos que espelham os modelos Pydantic do pipeline Python -- NENHUMA
 * estrutura de dado nova é inventada aqui, só o formato de leitura do que
 * `orchestrator.py`, `evidence_agent.py` e `registry.py` já gravam no
 * Firestore. Ver os campos originais:
 *
 *   - Investigation:        plane2_agents/orchestrator.py::_save_investigation
 *   - EvidenceBundle e afins: evidence_agent.py (mesmos nomes de campo)
 *   - AgentManifest:        registry.py::AgentManifest
 *
 * Os campos abaixo marcados "(dashboard)" são os únicos acrescentados por
 * este app -- gravados via `approveTakedown`/`rejectInvestigation`
 * (src/app/review/actions.ts), nunca lidos por nenhum código Python.
 */

export type Classification = "MALICIOUS" | "SAFE";

export type DossierStatus =
  | "PENDING_HUMAN_REVIEW"
  | "TAKEDOWN_APPROVED"
  | "REJECTED";

export type TakedownChannel =
  | "registrar_abuse"
  | "hosting_abuse"
  | "brand_protection_vendor";

export interface ArtifactRef {
  gcs_uri: string;
  sha256: string;
  content_type: string;
  size_bytes: number;
}

export interface HttpResponseSnapshot {
  status_code: number | null;
  headers: Record<string, string>;
  redirect_chain: string[];
  final_url: string | null;
}

export interface DnsRecords {
  a: string[];
  aaaa: string[];
  ns: string[];
  mx: string[];
  txt: string[];
}

export interface HostingInfo {
  ip_address: string | null;
  asn: number | null;
  asn_org: string | null;
}

export interface TlsCertificateInfo {
  issuer: string | null;
  subject: string | null;
  not_before: string | null;
  not_after: string | null;
  san: string[];
}

export interface RdapInfo {
  registrar: string | null;
  domain_created_at: string | null;
  domain_age_hours: number | null;
  abuse_contacts: string[];
}

export interface InfrastructureFingerprint {
  html_template_hash: string | null;
  ip_address: string | null;
  asn: number | null;
  registrar: string | null;
  cert_issuer: string | null;
  fingerprint_hash: string | null;
}

export interface FormFieldSignal {
  detected: boolean;
  field_count: number;
}

export interface CollectionError {
  step: string;
  error: string;
}

export interface EvidenceBundle {
  domain: string;
  collected_at: string;
  screenshot: ArtifactRef | null;
  html_snapshot: ArtifactRef | null;
  http_response: HttpResponseSnapshot | null;
  dns_records: DnsRecords | null;
  hosting: HostingInfo | null;
  tls_certificate: TlsCertificateInfo | null;
  rdap: RdapInfo | null;
  infrastructure_fingerprint: InfrastructureFingerprint | null;
  pii_redacted: Record<string, number>;
  form_fields_detected: FormFieldSignal;
  collection_errors: CollectionError[];
  is_partial: boolean;
  manifest_root_hash: string;
}

/** Documento `investigations/{domain}` -- união do que `orchestrator.py`
 * grava sempre e do que `evidence_agent.py`/este dashboard acrescentam
 * depois (por isso tantos campos opcionais: um dossiê recém-investigado
 * ainda não tem `evidence`, um MALICIOUS recém-coletado ainda não tem
 * `status`/decisão). */
export interface Investigation {
  domain: string;
  matched_brand: string | null;
  classification: Classification;
  confidence: number;
  reasoning: string;
  model: string;
  input_tokens: number;
  output_tokens: number;
  latency_ms: number;
  estimated_cost_usd: number;
  investigated_at: string;
  injection_signals: string[];
  pii_redacted: Record<string, number>;
  delimiter_escape_attempted: boolean;
  requires_human_review: boolean;
  agent_id: string;
  agent_version: string;

  // evidence_agent.py
  evidence?: EvidenceBundle;
  status?: DossierStatus;
  evidence_agent_id?: string;
  evidence_agent_version?: string;

  // (dashboard) -- ver src/app/review/actions.ts
  approved_by?: string;
  approved_at?: string;
  decision_rationale?: string;
  takedown_channel?: TakedownChannel;
  rejected_by?: string;
  rejected_at?: string;
  rejection_reason?: string;
}

export type AgentStatus = "ACTIVE" | "DEPRECATED" | "DISABLED";

/** Espelha `registry.AgentManifest` -- usado aqui só para descobrir e
 * validar o contrato de `takedown-agent` antes de publicar em
 * `takedown-approved` (mesmo papel de `registry.invoke_agent` no lado
 * Python, ver src/lib/takedown-registry.ts). */
export interface AgentManifest {
  agent_id: string;
  version: string;
  owner_team: string;
  description: string;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  tools_allowed: string[];
  required_permissions: string[];
  sla_seconds: number;
  status: AgentStatus;
  created_at: string;
}

/** Documento `metrics/pipeline_totals` -- mesmos contadores de
 * `telemetry.py::_COUNTER_NAMES`. */
export interface PipelineTotals {
  certificates_ingested_total?: number;
  certificates_discarded_by_prefilter_total?: number;
  llm_invocations_total?: number;
  cache_hits_total?: number;
  tokens_consumed_total?: number;
  estimated_cost_usd_total?: number;
  gemma_triage_total?: number;
  gemma_discarded_total?: number;
  gemma_escalated_total?: number;
  gemma_fallback_total?: number;
  gemma_triage_cost_usd_total?: number;
  evidence_bundles_collected_total?: number;
  evidence_bundles_partial_total?: number;
}
