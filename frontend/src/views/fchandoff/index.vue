<template>
  <section class="page" data-module="fchandoff">
    <header class="page-head">
      <div>
        <h2>预测交接版本链</h2>
        <p class="page-desc">
          预测方案部署时固化预测窗口、特征字段与模型版本；分析方案只消费已部署版本，
          输出记录预测结果、置信水平与对比基线，换版全程写审计。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="exportPackage">导出交接包</button>
        <button class="btn" type="button" @click="reloadPackage">重新载入</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="deployCaliber">
      <label class="filter-item">
        <span>预测目标</span>
        <input v-model="deployForm.target_entity" />
      </label>
      <label class="filter-item">
        <span>预测窗口天</span>
        <input v-model="deployForm.forecast_window_days" type="number" min="1" />
      </label>
      <label class="filter-item">
        <span>特征字段</span>
        <input v-model="deployForm.feature_set" />
      </label>
      <label class="filter-item">
        <span>模型版本</span>
        <input v-model="deployForm.model_version" />
      </label>
      <label class="filter-item">
        <span>置信水平</span>
        <input v-model="deployForm.confidence_level" type="number" step="0.01" min="0.01" max="1" />
      </label>
      <button class="btn primary" type="submit">部署固化口径</button>
      <button class="btn" type="button" @click="retrain(true)">重训成功换版</button>
      <button class="btn ghost" type="button" @click="retrain(false)">标记训练失败</button>
    </form>

    <form class="filter-bar" @submit.prevent="consume">
      <label class="filter-item">
        <span>分析方案</span>
        <input v-model="consumeForm.analysis_plan" />
      </label>
      <label class="filter-item">
        <span>交接信封（仅已部署生效）</span>
        <select v-model="consumeForm.envelope_id">
          <option v-for="env in envelopes" :key="env.envelope_id" :value="env.envelope_id">
            {{ env.envelope_id }}（{{ env.status }}）
          </option>
        </select>
      </label>
      <label class="filter-item">
        <span>预测日期</span>
        <input v-model="consumeForm.forecast_date" type="date" />
      </label>
      <label class="filter-item">
        <span>预测结果</span>
        <input v-model="consumeForm.forecast_value" type="number" />
      </label>
      <label class="filter-item">
        <span>对比基线</span>
        <input v-model="consumeForm.baseline_value" type="number" />
      </label>
      <button class="btn primary" type="submit">生成分析输出</button>
    </form>

    <h3 class="section-title">交接信封（部署即固化的口径）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in envelopeColumns" :key="column.key">{{ column.label }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in envelopes" :key="row.envelope_id">
          <td v-for="column in envelopeColumns" :key="column.key">
            <span v-if="column.key === 'status'" class="badge" :data-status="row.status">{{ row.status }}</span>
            <template v-else>{{ row[column.key] ?? '—' }}</template>
          </td>
        </tr>
        <tr v-if="!envelopes.length">
          <td :colspan="envelopeColumns.length" class="empty-state">暂无交接信封，部署预测方案后自动生成</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">分析输出（预测结果 + 置信水平 + 对比基线）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in outputColumns" :key="column.key">{{ column.label }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in outputs" :key="row.output_id">
          <td v-for="column in outputColumns" :key="column.key">
            <template v-if="column.key === 'sample_range'">{{ formatRange(row.sample_range) }}</template>
            <template v-else-if="column.key === 'affected'">
              <span v-if="row.affected" class="badge" data-status="affected" :title="row.affected_reason">受影响</span>
              <template v-else>正常</template>
            </template>
            <template v-else>{{ row[column.key] ?? '—' }}</template>
          </td>
        </tr>
        <tr v-if="!outputs.length">
          <td :colspan="outputColumns.length" class="empty-state">暂无分析输出，可先在上方生成</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">审计事件（deploy / supersede / reject）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in auditColumns" :key="column.key">{{ column.label }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in audit" :key="row.id">
          <td v-for="column in auditColumns" :key="column.key">{{ row[column.key] ?? '—' }}</td>
        </tr>
        <tr v-if="!audit.length">
          <td :colspan="auditColumns.length" class="empty-state">暂无审计事件</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ envelopes.length }} 封信封 · {{ outputs.length }} 条输出 · {{ audit.length }} 条审计</span>
      <span v-if="notice" class="notice-text">{{ notice }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchJson, request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/fchandoff'

const envelopeColumns = [
  { key: 'envelope_id', label: '信封编号' },
  { key: 'target_entity', label: '预测目标' },
  { key: 'forecast_window_days', label: '预测窗口天' },
  { key: 'feature_set', label: '特征字段' },
  { key: 'model_version', label: '模型版本' },
  { key: 'confidence_interval', label: '置信区间' },
  { key: 'deploy_marker', label: '部署标记' },
  { key: 'badge', label: '徽标' },
  { key: 'status', label: '状态' },
]
const outputColumns = [
  { key: 'output_id', label: '输出编号' },
  { key: 'analysis_plan', label: '分析方案' },
  { key: 'forecast_date', label: '预测日期' },
  { key: 'forecast_value', label: '预测结果' },
  { key: 'confidence', label: '置信水平' },
  { key: 'baseline_value', label: '对比基线' },
  { key: 'model_version', label: '模型版本' },
  { key: 'sample_range', label: '样本范围' },
  { key: 'badge', label: '徽标' },
  { key: 'affected', label: '受影响' },
]
const auditColumns = [
  { key: 'event', label: '事件' },
  { key: 'envelope_id', label: '交接信封' },
  { key: 'model_version', label: '模型版本' },
  { key: 'actor', label: '操作人' },
  { key: 'at', label: '时间' },
  { key: 'detail', label: '说明' },
]

const envelopes = ref<Row[]>([])
const outputs = ref<Row[]>([])
const audit = ref<Row[]>([])
const errorMessage = ref('')
const notice = ref('')

const deployForm = ref({
  target_entity: 'GMV_DAILY',
  algorithm_label: 'Prophet',
  feature_set: 'channel,discount,campaign',
  forecast_window_days: 14,
  train_cycle_days: 7,
  confidence_level: 0.95,
  model_version: 'model-v9',
})
const consumeForm = ref({
  analysis_plan: 'ANAL-GMV_TREND',
  envelope_id: 'analysis_handoff_v8',
  forecast_date: '2026-10-16',
  forecast_value: 1291000,
  baseline_value: 1203000,
})

const stats = computed(() => [
  { label: '当前部署版本', value: envelopes.value.filter((row) => row.status === 'deployed').length },
  { label: '交接信封', value: envelopes.value.length },
  { label: '分析输出', value: outputs.value.length },
  { label: '审计事件', value: audit.value.length },
])

function formatRange(range: Row | null) {
  if (!range || !range.start) return '—'
  return `${range.start} ~ ${range.end}`
}

async function post(path: string, values: Row) {
  errorMessage.value = ''
  notice.value = ''
  const response = await request(`${ENDPOINT}${path}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  const payload = await response.json()
  if (!payload.ok) {
    throw new Error(payload.message || '操作未生效')
  }
  notice.value = payload.message
  return payload
}

async function deployCaliber() {
  try {
    await post('/deploy', { ...deployForm.value })
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '部署失败'
  }
}

async function retrain(trainingOk: boolean) {
  errorMessage.value = ''
  notice.value = ''
  try {
    const response = await request(`${ENDPOINT}/retrain`, {
      method: 'POST',
      body: JSON.stringify({
        values: {
          target_entity: deployForm.value.target_entity,
          forecast_window_days: Number(deployForm.value.forecast_window_days),
          new_model_version: deployForm.value.model_version,
          training_ok: trainingOk,
          window_complete: true,
        },
      }),
    })
    const payload = await response.json()
    // 训练失败是业务结果（只标记受影响输出），展示后端说明而不是报错
    notice.value = payload.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重训失败'
  }
}

async function consume() {
  try {
    await post('/outputs', { ...consumeForm.value })
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '生成分析输出失败'
  }
}

function exportPackage() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function reloadPackage() {
  errorMessage.value = ''
  notice.value = ''
  try {
    const pkg = await fetchJson<Row>(`${ENDPOINT}/export`)
    const response = await request(`${ENDPOINT}/reload`, {
      method: 'POST',
      body: JSON.stringify({ values: { package: pkg } }),
    })
    const payload = await response.json()
    notice.value = payload.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '交接包重新载入失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const [envPage, outPage, auditPage] = await Promise.all([
      fetchJson<{ items: Row[] }>(`${ENDPOINT}/envelopes`),
      fetchJson<{ items: Row[] }>(`${ENDPOINT}/outputs`),
      fetchJson<{ items: Row[] }>(`${ENDPOINT}/audit`),
    ])
    envelopes.value = envPage.items ?? []
    outputs.value = outPage.items ?? []
    audit.value = (auditPage.items ?? []).slice().reverse()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '版本链数据读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.section-title {
  font-size: 14px;
  margin: 16px 0 8px;
}
.badge {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 10px;
  font-size: 12px;
  background: #e7f0fe;
  color: var(--brand);
}
.badge[data-status='superseded'] {
  background: #f1f5f9;
  color: var(--muted);
}
.badge[data-status='affected'] {
  background: #fef3c7;
  color: #92400e;
}
.notice-text {
  color: #047857;
}
select {
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 4px 6px;
}
</style>
