<template>
  <section class="page" data-testid="slot-migration">
    <header class="page-head">
      <div>
        <h2>槽位迁移</h2>
        <p class="page-desc">
          v1 字典字符串与 v2 结构化槽位并行迁移：先演练、再暂停/恢复，语义入库只接纳已核对条目。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="migration-start" type="button" :disabled="busy" @click="action('开始迁移')">
          开始迁移
        </button>
        <button class="btn" data-testid="migration-pause" type="button" :disabled="busy" @click="action('暂停')">
          暂停
        </button>
        <button class="btn" data-testid="migration-resume" type="button" :disabled="busy" @click="action('恢复')">
          恢复
        </button>
        <button class="btn" data-testid="migration-rollback" type="button" :disabled="busy" @click="action('回滚')">
          回滚
        </button>
      </div>
    </header>

    <p v-if="message" class="status-legend" data-testid="migration-message">{{ message }}</p>
    <p v-if="errorMessage" class="error-text" data-testid="migration-error">{{ errorMessage }}</p>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">批次状态</span>
        <strong class="stat-value" data-testid="batch-status">{{ state?.batch.status ?? '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已迁移交换记录</span>
        <strong class="stat-value">{{ state?.exchange_count ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已转换 / 已结构化</span>
        <strong class="stat-value">{{ convertedCount }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待补正</span>
        <strong class="stat-value">{{ pendingCount }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">语义入库接纳</span>
        <strong class="stat-value">{{ state?.acceptance_count ?? 0 }}</strong>
      </article>
    </div>

    <div class="filter-bar">
      <button class="btn" data-testid="migration-step" type="button" :disabled="busy" @click="migrateStep">
        执行一步迁移
      </button>
      <button class="btn" data-testid="migration-ingest" type="button" :disabled="busy" @click="ingest">
        执行语义入库
      </button>
      <button class="btn ghost" data-testid="migration-refresh" type="button" :disabled="busy" @click="reload">
        刷新
      </button>
      <label class="filter-item">
        <span>交换记录状态</span>
        <select v-model="exchangeFilter" data-testid="exchange-filter">
          <option value="">全部</option>
          <option value="converted">已转换</option>
          <option value="already_structured">已结构化</option>
          <option value="needs_review">待补正</option>
          <option value="unconvertible">不可转换</option>
        </select>
      </label>
    </div>

    <h3 class="section-title">标注交接清单</h3>
    <table class="data-table" data-testid="manifest-table">
      <thead>
        <tr>
          <th>情感记录</th>
          <th>片段原文</th>
          <th>片段出处</th>
          <th>语义记录</th>
          <th>原版本 → 交换版本</th>
          <th>槽位状态</th>
          <th>槽位类型</th>
          <th>入库</th>
          <th>补正原因</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="item in manifest?.items ?? []"
          :key="item.exchange_id"
          :data-testid="`manifest-${item.semantic_id}`"
          :data-status="item.status"
        >
          <td>{{ item.sentiment_record_id }}</td>
          <td>{{ item.snippet }}</td>
          <td>{{ item.snippet_source }}</td>
          <td>{{ item.semantic_id }}</td>
          <td>{{ item.original_version }} → {{ item.conversion_version }}</td>
          <td>{{ item.status_label }}</td>
          <td>{{ item.slot_types.join('、') || '—' }}</td>
          <td>{{ item.accepted ? '已接纳' : '未接纳' }}</td>
          <td>{{ item.needs_correction ? `${item.reason || '待核对'}：${item.detail}` : '—' }}</td>
        </tr>
        <tr v-if="!(manifest?.items ?? []).length">
          <td colspan="9" class="empty-state">还没有迁移记录，先执行一次迁移</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">带版本的语义槽位交换数据</h3>
    <table class="data-table" data-testid="exchange-table">
      <thead>
        <tr>
          <th>交换ID</th>
          <th>语义记录</th>
          <th>情感来源</th>
          <th>原版本</th>
          <th>交换版本</th>
          <th>槽位数</th>
          <th>状态</th>
          <th>旧版回读</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in filteredExchanges" :key="item.exchange_id">
          <td>{{ item.exchange_id }}</td>
          <td>{{ item.semantic_id }}</td>
          <td>{{ item.source_sentiment_id }}</td>
          <td>{{ item.version }}</td>
          <td>{{ item.schema_version }}</td>
          <td>{{ item.slot_count }}</td>
          <td>{{ item.status_label }}</td>
          <td>
            <button class="link" type="button" @click="readLegacy(item.exchange_id)">按旧版读取</button>
          </td>
        </tr>
        <tr v-if="!filteredExchanges.length">
          <td colspan="8" class="empty-state">暂无交换数据</td>
        </tr>
      </tbody>
    </table>

    <pre v-if="legacyReadback" class="legacy-output" data-testid="legacy-readback">{{ legacyReadback }}</pre>

    <h3 class="section-title">最近一次语义入库回执</h3>
    <div v-if="lastRun" data-testid="ingest-receipt">
      <p class="status-legend">
        运行 {{ lastRun.run_id }}：接纳 {{ lastRun.accepted_count }} 条，跳过 {{ lastRun.skipped_count }} 条
      </p>
      <table class="data-table">
        <thead>
          <tr><th>语义记录</th><th>输入版本</th><th>情感来源</th><th>槽位</th><th>结论</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in lastRun.accepted" :key="item.acceptance_id" data-testid="accepted-row">
            <td>{{ item.semantic_id }}</td>
            <td>{{ item.input_version }}</td>
            <td>{{ item.source_sentiment_id }}</td>
            <td>{{ item.slots.map((slot) => slot.slot_type).join('、') }}</td>
            <td>已接纳</td>
          </tr>
          <tr
            v-for="item in lastRun.needs_correction"
            :key="`pending-${item.semantic_id}`"
            data-testid="correction-row"
          >
            <td>{{ item.semantic_id }}</td>
            <td>{{ item.input_version ?? '—' }}</td>
            <td>{{ item.source_sentiment_id ?? '—' }}</td>
            <td>—</td>
            <td>须回情感标注补正：{{ item.reason }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-else class="status-legend">尚未执行语义入库</p>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  type ExchangeSummary,
  type IngestRun,
  type MigrationManifest,
  type MigrationState,
  loadExchanges,
  loadIngestRuns,
  loadManifest,
  loadMigrationState,
  readLegacySlots,
  runIngest,
  runMigrateStep,
  runMigrationAction,
} from '@/api/migration-service'

const state = ref<MigrationState | null>(null)
const manifest = ref<MigrationManifest | null>(null)
const exchanges = ref<ExchangeSummary[]>([])
const lastRun = ref<IngestRun | null>(null)
const exchangeFilter = ref('')
const legacyReadback = ref('')
const message = ref('')
const errorMessage = ref('')
const busy = ref(false)

const convertedCount = computed(() => {
  const counts = state.value?.status_counts
  return counts ? counts.converted + counts.already_structured : 0
})
const pendingCount = computed(() => {
  const counts = state.value?.status_counts
  return counts ? counts.needs_review + counts.unconvertible : 0
})
const filteredExchanges = computed(() =>
  exchangeFilter.value
    ? exchanges.value.filter((item) => item.status === exchangeFilter.value)
    : exchanges.value,
)

async function reload() {
  errorMessage.value = ''
  try {
    const [nextState, nextManifest, nextExchanges, runs] = await Promise.all([
      loadMigrationState(),
      loadManifest(),
      loadExchanges(),
      loadIngestRuns(),
    ])
    state.value = nextState
    manifest.value = nextManifest
    exchanges.value = nextExchanges.items
    lastRun.value = runs.runs.length ? runs.runs[runs.runs.length - 1] : null
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '迁移状态读取失败'
  }
}

async function withBusy(task: () => Promise<string>) {
  busy.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    message.value = await task()
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  } finally {
    busy.value = false
  }
}

function action(name: string) {
  void withBusy(async () => (await runMigrationAction(name)).message)
}

function migrateStep() {
  void withBusy(async () => {
    await runMigrateStep()
    return '已执行一步迁移'
  })
}

function ingest() {
  void withBusy(async () => {
    const run = await runIngest()
    return `入库完成：接纳 ${run.accepted_count} 条，须补正 ${run.needs_correction.length} 条`
  })
}

async function readLegacy(exchangeId: string) {
  errorMessage.value = ''
  try {
    const payload = await readLegacySlots(exchangeId)
    legacyReadback.value = [
      `交换记录 ${exchangeId}（${payload.status}）`,
      `原版本 ${payload.original_version}，已核对 ${payload.verified ? '是' : '否'}`,
      `原值：${String(payload.original)}`,
      `旧版回读：${payload.legacy_repr}`,
    ].join('\n')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '旧版回读失败'
  }
}

onMounted(reload)
</script>
