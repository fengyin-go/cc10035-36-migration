/** 槽位迁移工作台接口：与后端 /api/nlu-slot-migration 一一对应。 */
import { request } from './client'

export type MigrationStatusCounts = {
  already_structured: number
  converted: number
  needs_review: number
  unconvertible: number
}

export type MigrationBatch = {
  batch_id: string | null
  name: string
  status: string
  created_at: string | null
  updated_at: string | null
  cursor: number
  total: number
  processed: string[]
  revoked_acceptances?: number
}

export type ManifestItem = {
  sentiment_record_id: string
  snippet: string
  snippet_source: string
  semantic_id: string
  exchange_id: string
  original_version: string
  conversion_version: string
  status: string
  status_label: string
  needs_correction: boolean
  reason: string
  detail: string
  batch_id: string | null
  accepted: boolean
  slot_types: string[]
}

export type ExchangeSummary = {
  exchange_id: string
  semantic_id: string
  source_sentiment_id: string
  version: string
  schema_version: string
  status: string
  status_label: string
  slot_count: number
  needs_correction: boolean
}

export type AcceptanceSlot = {
  slot_id: string
  slot_type: string
  value: unknown
  value_type: string
  provenance_type: string
  sentiment_record_id: string
  snippet_sha256: string
  snippet_source: string
}

export type Acceptance = {
  acceptance_id: string
  run_id: string
  exchange_id: string
  semantic_id: string
  input_version: string
  accepted_version: string
  status: string
  accepted_at: string
  slot_count: number
  slots: AcceptanceSlot[]
  source_sentiment_id: string
  snippet: string
  snippet_sha256: string
}

export type SkippedItem = {
  semantic_id: string
  exchange_id: string
  reason: string
  detail: string
  needs_sentiment_correction: boolean
  source_sentiment_id?: string
  snippet?: string
  input_version?: string
}

export type IngestRun = {
  run_id: string
  batch_id: string | null
  executed_at: string
  accepted: Acceptance[]
  skipped: SkippedItem[]
  accepted_count: number
  skipped_count: number
  needs_correction: SkippedItem[]
}

export type MigrationState = {
  batch: MigrationBatch
  total_records: number
  exchange_count: number
  status_counts: MigrationStatusCounts
  manifest_count: number
  needs_correction: ManifestItem[]
  acceptance_count: number
  acceptance_revoked: number
  last_ingest_run: IngestRun | null
  batch_history: { batch: MigrationBatch }[]
}

export type MigrationManifest = {
  items: ManifestItem[]
  total: number
  pending_correction: ManifestItem[]
  converted: ManifestItem[]
}

const BASE = '/api/nlu-slot-migration'

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await request(`${BASE}${path}`, init)
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(
      typeof payload.detail === 'string' ? payload.detail : `接口返回 ${response.status}`,
    )
  }
  return payload as T
}

export function loadMigrationState(): Promise<MigrationState> {
  return call<MigrationState>('/state')
}

export function loadManifest(): Promise<MigrationManifest> {
  return call<MigrationManifest>('/manifest')
}

export function loadExchanges(status?: string): Promise<{ items: ExchangeSummary[]; total: number }> {
  const query = status ? `?status=${encodeURIComponent(status)}` : ''
  return call<{ items: ExchangeSummary[]; total: number }>(`/exchanges${query}`)
}

export function runMigrationAction(action: string): Promise<{ ok: boolean; message: string }> {
  return call<{ ok: boolean; message: string }>('/action', {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
}

export async function runMigrateStep(limit?: number): Promise<void> {
  await call('/migrate', {
    method: 'POST',
    body: JSON.stringify(limit ? { limit } : {}),
  })
}

export function runIngest(runId?: string): Promise<IngestRun & { ok: boolean }> {
  return call<IngestRun & { ok: boolean }>('/ingest', {
    method: 'POST',
    body: JSON.stringify(runId ? { run_id: runId } : {}),
  })
}

export function loadIngestRuns(): Promise<{
  runs: IngestRun[]
  active_acceptances: Acceptance[]
  revoked_acceptances: Acceptance[]
  active_by_input_version: Record<string, number>
}> {
  return call('/ingest/runs')
}

export function readLegacySlots(exchangeId: string): Promise<{
  ok: boolean
  semantic_id: string
  original_version: string
  original: unknown
  legacy_repr: string
  verified: boolean
  lossless: boolean
  status: string
}> {
  return call(`/exchanges/${encodeURIComponent(exchangeId)}/legacy`)
}
