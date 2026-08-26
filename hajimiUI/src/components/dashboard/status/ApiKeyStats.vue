<script setup>
import { useDashboardStore } from '../../../stores/dashboard'
import { computed, ref, onUnmounted, watch } from 'vue'
import ApiCallsChart from './ApiCallsChart.vue'

const dashboardStore = useDashboardStore()

// ---------- UI state ----------
const showTable = ref(true)
const collapsedKeys = ref({}) // key hash → bool

const dialogState = ref({
  add: false,
  test: false,
  clear: false,
  export: false,
})

function openDialog(name) {
  // close any other open dialog
  Object.keys(dialogState.value).forEach((k) => (dialogState.value[k] = false))
  dialogState.value[name] = true
}
function closeDialog(name) {
  dialogState.value[name] = false
}

// ---------- Add keys ----------
const newApiKeys = ref('')
const addError = ref('')
const addSuccess = ref('')
const isAdding = ref(false)

async function submitAdd() {
  addError.value = ''
  addSuccess.value = ''
  if (!newApiKeys.value.trim()) {
    addError.value = 'Enter at least one key'
    return
  }
  const keys = newApiKeys.value
    .split(/[,\n\r]+/)
    .map((s) => s.trim())
    .filter(Boolean)
  if (keys.some((k) => k.length < 10)) {
    addError.value = 'One or more keys look too short'
    return
  }
  isAdding.value = true
  try {
    await dashboardStore.updateConfig('geminiApiKeys', newApiKeys.value, dashboardStore.sessionPassword)
    addSuccess.value = `Added ${keys.length} key(s)`
    newApiKeys.value = ''
    await dashboardStore.fetchDashboardData()
    setTimeout(() => closeDialog('add'), 1500)
  } catch (e) {
    addError.value = e.message || 'Failed to add keys'
  } finally {
    isAdding.value = false
  }
}

// ---------- Test keys ----------
const testPassword = ref('')
const testError = ref('')
const testProgress = ref({ completed: 0, total: 0, valid: 0, invalid: 0, is_completed: false })
const isTesting = ref(false)
let testPollTimer = null
// 连续失败计数：后端重启/任务丢失时 is_completed 永不到达，
// 轮询必须自己设上限，否则每 1.5s 永久打接口。
const MAX_POLL_FAILURES = 10
let pollFailures = 0

function stopPolling() {
  if (testPollTimer) {
    clearInterval(testPollTimer)
    testPollTimer = null
  }
}

// 卸载清理：切换页面/组件销毁时停掉轮询定时器（此前无清理，
// 离开控制台后仍每 1.5s 请求一次）。
onUnmounted(stopPolling)

async function startTest() {
  testError.value = ''
  if (!testPassword.value) {
    testError.value = 'Password required'
    return
  }
  isTesting.value = true
  testProgress.value = { completed: 0, total: 0, valid: 0, invalid: 0, is_completed: false }
  try {
    const response = await fetch('/api/test-api-keys', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: testPassword.value }),
    })
    if (!response.ok) {
      const err = await response.json().catch(() => ({}))
      throw new Error(err.detail || `HTTP ${response.status}`)
    }
    testPollTimer = setInterval(pollTestProgress, 1500)
  } catch (e) {
    testError.value = e.message || 'Failed to start test'
    isTesting.value = false
  }
}

async function pollTestProgress() {
  try {
    // 凭证只走 Authorization 头，不再拼 URL 查询串（查询串会进
    // uvicorn access log 与反代日志）。
    const r = await fetch('/api/test-api-keys/progress', {
      headers: dashboardStore.authHeaders(),
    })
    if (!r.ok) {
      // 非 200：连续 N 次后放弃轮询，避免后端重启后永久空转
      if (++pollFailures >= MAX_POLL_FAILURES) {
        stopPolling()
        isTesting.value = false
        testError.value = 'Progress polling failed repeatedly; test may have been interrupted'
      }
      return
    }
    pollFailures = 0
    const data = await r.json()
    testProgress.value = data
    if (data.is_completed) {
      stopPolling()
      isTesting.value = false
      await dashboardStore.fetchDashboardData()
    }
  } catch (e) {
    // 网络错误同样计入失败上限
    if (++pollFailures >= MAX_POLL_FAILURES) {
      stopPolling()
      isTesting.value = false
      testError.value = 'Progress polling failed repeatedly; test may have been interrupted'
    }
  }
}

// ---------- Clear invalid ----------
const clearPassword = ref('')
const clearError = ref('')
const clearSuccess = ref('')
const isClearing = ref(false)

async function submitClear() {
  clearError.value = ''
  clearSuccess.value = ''
  if (!clearPassword.value) {
    clearError.value = 'Password required'
    return
  }
  isClearing.value = true
  try {
    const r = await fetch('/api/clear-invalid-api-keys', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: clearPassword.value }),
    })
    const data = await r.json()
    if (!r.ok) throw new Error(data.detail || 'Failed')
    clearSuccess.value = data.message || 'Cleared'
    clearPassword.value = ''
    await dashboardStore.fetchDashboardData()
    setTimeout(() => closeDialog('clear'), 1500)
  } catch (e) {
    clearError.value = e.message || 'Failed'
  } finally {
    isClearing.value = false
  }
}

// ---------- Export valid ----------
const exportPassword = ref('')
const exportError = ref('')
const exportedKeys = ref([])
const isExporting = ref(false)

async function submitExport() {
  exportError.value = ''
  exportedKeys.value = []
  if (!exportPassword.value) {
    exportError.value = 'Password required'
    return
  }
  isExporting.value = true
  try {
    const r = await fetch('/api/export-valid-api-keys', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: exportPassword.value }),
    })
    const data = await r.json()
    if (!r.ok) throw new Error(data.detail || 'Failed')
    exportedKeys.value = data.keys || []
    exportPassword.value = ''
  } catch (e) {
    exportError.value = e.message || 'Failed'
  } finally {
    isExporting.value = false
  }
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text)
  } catch (e) {
    console.error('copy:', e)
  }
}

// ---------- Table ----------
const currentPage = ref(1)
const pageSize = 10

const totalPages = computed(() =>
  Math.max(1, Math.ceil(dashboardStore.apiKeyStats.length / pageSize))
)

// 数据缩减（如 Clear invalid 删除无效 key）后钳制当前页码，
// 否则停在超界页会显示假空态。
watch(totalPages, (tp) => {
  if (currentPage.value > tp) currentPage.value = tp
})

const pageRows = computed(() => {
  const start = (currentPage.value - 1) * pageSize
  return dashboardStore.apiKeyStats.slice(start, start + pageSize)
})

const totalCalls = computed(() =>
  dashboardStore.apiKeyStats.reduce((s, k) => s + (k.calls_24h || 0), 0)
)
const totalTokens = computed(() =>
  dashboardStore.apiKeyStats.reduce((s, k) => s + (k.total_tokens || 0), 0)
)

function fmt(n) {
  if (n == null) return '0'
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B'
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M'
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k'
  return String(n)
}

function modelList(stat) {
  return Object.entries(stat.model_stats || {})
    .map(([m, d]) => ({ m, calls: d.calls || 0, tokens: d.tokens || 0 }))
    .sort((a, b) => b.calls - a.calls)
}

function toggleModels(keyId) {
  collapsedKeys.value[keyId] = !collapsedKeys.value[keyId]
}
</script>

<template>
  <div class="card mt-4" v-if="!dashboardStore.status.enableVertex">
    <div class="card__header">
      <div>
        <div class="card__title">API Keys</div>
        <div class="card__subtitle">
          {{ dashboardStore.apiKeyStats.length }} keys ·
          {{ fmt(totalCalls) }} calls · {{ fmt(totalTokens) }} tokens (24h)
        </div>
      </div>
      <div class="row">
        <button class="btn btn--secondary btn--sm" @click="openDialog('add')">+ Add</button>
        <button class="btn btn--secondary btn--sm" @click="openDialog('test')" :disabled="isTesting">
          {{ isTesting ? 'Testing…' : 'Test' }}
        </button>
        <button class="btn btn--secondary btn--sm" @click="openDialog('clear')">Clear invalid</button>
        <button class="btn btn--secondary btn--sm" @click="openDialog('export')">Export</button>
        <button class="btn btn--ghost btn--sm btn--icon" @click="showTable = !showTable" :title="showTable ? 'Collapse' : 'Expand'">
          {{ showTable ? '▾' : '▸' }}
        </button>
      </div>
    </div>

    <!-- ============== Chart ============== -->
    <div v-if="showTable" class="card__body" style="padding:0;">
      <ApiCallsChart />

      <!-- ============== Table ============== -->
      <div style="overflow-x:auto;">
        <table class="table">
          <thead>
            <tr>
              <th>Key</th>
              <th style="text-align:right;">Calls 24h</th>
              <th style="text-align:right;">Tokens</th>
              <th>Models</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="stat in pageRows" :key="stat.api_key">
              <tr>
                <td class="mono">{{ stat.api_key }}</td>
                <td style="text-align:right;" class="mono">{{ fmt(stat.calls_24h) }}</td>
                <td style="text-align:right;" class="mono">{{ fmt(stat.total_tokens) }}</td>
                <td>
                  <button
                    v-if="modelList(stat).length"
                    class="btn btn--ghost btn--sm"
                    @click="toggleModels(stat.api_key)"
                  >
                    {{ modelList(stat).length }} models {{ collapsedKeys[stat.api_key] ? '▴' : '▾' }}
                  </button>
                  <span v-else class="text-subtle">—</span>
                </td>
              </tr>
              <tr v-if="collapsedKeys[stat.api_key]">
                <td colspan="4" style="padding:0;border:none;">
                  <div class="model-subtable">
                    <table class="table" style="background:transparent;">
                      <thead>
                        <tr>
                          <th>Model</th>
                          <th style="text-align:right;">Calls</th>
                          <th style="text-align:right;">Tokens</th>
                        </tr>
                      </thead>
                      <tbody>
                        <tr v-for="m in modelList(stat)" :key="m.m">
                          <td class="mono">{{ m.m }}</td>
                          <td class="mono" style="text-align:right;">{{ fmt(m.calls) }}</td>
                          <td class="mono" style="text-align:right;">{{ fmt(m.tokens) }}</td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                </td>
              </tr>
            </template>
            <tr v-if="!pageRows.length">
              <td colspan="4">
                <div class="empty-state" style="padding:var(--sp-8);">
                  <div class="empty-state__icon">∅</div>
                  <div class="empty-state__title">No keys in pool</div>
                  <div class="empty-state__desc">Use “+ Add” to import keys.</div>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- ============== Pagination ============== -->
      <div v-if="totalPages > 1" class="row row--between" style="padding:var(--sp-3) var(--sp-5);border-top:1px solid var(--border);">
        <span class="text-muted" style="font-size:var(--fs-sm);">
          Page {{ currentPage }} of {{ totalPages }}
        </span>
        <div class="row">
          <button class="btn btn--secondary btn--sm" @click="currentPage--" :disabled="currentPage === 1">‹</button>
          <button class="btn btn--secondary btn--sm" @click="currentPage++" :disabled="currentPage === totalPages">›</button>
        </div>
      </div>
    </div>
  </div>

  <!-- ============== Add dialog ============== -->
  <div v-if="dialogState.add" class="modal-overlay" @click.self="closeDialog('add')">
    <div class="modal">
      <div class="modal__header">
        <div class="modal__title">Add API keys</div>
        <button class="btn btn--ghost btn--icon btn--sm" @click="closeDialog('add')">✕</button>
      </div>
      <div class="modal__body">
        <div class="field mb-4">
          <label class="field__label">Keys (one per line or comma-separated)</label>
          <textarea v-model="newApiKeys" class="textarea" rows="6" placeholder="AIza…&#10;AIza…"></textarea>
        </div>
        <div v-if="addError" class="banner banner--danger">{{ addError }}</div>
        <div v-if="addSuccess" class="banner banner--success">{{ addSuccess }}</div>
      </div>
      <div class="modal__footer">
        <button class="btn btn--secondary btn--sm" @click="closeDialog('add')">Cancel</button>
        <button class="btn btn--primary btn--sm" @click="submitAdd" :disabled="isAdding">
          {{ isAdding ? 'Adding…' : 'Add keys' }}
        </button>
      </div>
    </div>
  </div>

  <!-- ============== Test dialog ============== -->
  <div v-if="dialogState.test" class="modal-overlay" @click.self="!isTesting && closeDialog('test')">
    <div class="modal">
      <div class="modal__header">
        <div class="modal__title">Test keys</div>
        <button class="btn btn--ghost btn--icon btn--sm" @click="closeDialog('test')" :disabled="isTesting">✕</button>
      </div>
      <div class="modal__body">
        <p class="text-muted mb-4" style="font-size:var(--fs-sm);">
          Probe every key in the pool to verify it still works. This counts against upstream rate limits.
        </p>
        <div class="field mb-4">
          <label class="field__label">Password</label>
          <input v-model="testPassword" type="password" class="input" autocomplete="current-password">
        </div>
        <div v-if="testError" class="banner banner--danger">{{ testError }}</div>
        <div v-if="testProgress.total > 0" class="mt-4">
          <div class="row row--between mb-2" style="font-size:var(--fs-sm);">
            <span class="text-muted">Progress</span>
            <span class="mono">{{ testProgress.completed }} / {{ testProgress.total }}</span>
          </div>
          <div class="progress">
            <div
              class="progress__bar"
              :style="{ width: ((testProgress.completed / Math.max(1, testProgress.total)) * 100) + '%' }"
            ></div>
          </div>
          <div class="row row--between mt-2" style="font-size:var(--fs-sm);">
            <span class="text-success">✓ {{ testProgress.valid }} valid</span>
            <span class="text-danger">✕ {{ testProgress.invalid }} invalid</span>
          </div>
        </div>
      </div>
      <div class="modal__footer">
        <button class="btn btn--secondary btn--sm" @click="closeDialog('test')" :disabled="isTesting">Cancel</button>
        <button class="btn btn--primary btn--sm" @click="startTest" :disabled="isTesting">
          {{ isTesting ? 'Testing…' : 'Start test' }}
        </button>
      </div>
    </div>
  </div>

  <!-- ============== Clear invalid dialog ============== -->
  <div v-if="dialogState.clear" class="modal-overlay" @click.self="closeDialog('clear')">
    <div class="modal">
      <div class="modal__header">
        <div class="modal__title">Clear invalid keys</div>
        <button class="btn btn--ghost btn--icon btn--sm" @click="closeDialog('clear')">✕</button>
      </div>
      <div class="modal__body">
        <p class="text-muted mb-4" style="font-size:var(--fs-sm);">
          Remove every key previously marked as invalid. This cannot be undone.
        </p>
        <div class="field">
          <label class="field__label">Password</label>
          <input v-model="clearPassword" type="password" class="input" autocomplete="current-password">
        </div>
        <div v-if="clearError" class="banner banner--danger mt-4">{{ clearError }}</div>
        <div v-if="clearSuccess" class="banner banner--success mt-4">{{ clearSuccess }}</div>
      </div>
      <div class="modal__footer">
        <button class="btn btn--secondary btn--sm" @click="closeDialog('clear')">Cancel</button>
        <button class="btn btn--primary btn--sm" @click="submitClear" :disabled="isClearing">
          {{ isClearing ? 'Clearing…' : 'Clear invalid' }}
        </button>
      </div>
    </div>
  </div>

  <!-- ============== Export dialog ============== -->
  <div v-if="dialogState.export" class="modal-overlay" @click.self="closeDialog('export')">
    <div class="modal" style="max-width:560px;">
      <div class="modal__header">
        <div class="modal__title">Export valid keys</div>
        <button class="btn btn--ghost btn--icon btn--sm" @click="closeDialog('export')">✕</button>
      </div>
      <div class="modal__body">
        <div class="field mb-4">
          <label class="field__label">Password</label>
          <input v-model="exportPassword" type="password" class="input" autocomplete="current-password">
        </div>
        <div v-if="exportError" class="banner banner--danger">{{ exportError }}</div>
        <div v-if="exportedKeys.length">
          <div class="row row--between mb-2">
            <span class="section__title">{{ exportedKeys.length }} valid keys</span>
            <button class="btn btn--secondary btn--sm" @click="copyText(exportedKeys.join('\n'))">
              Copy all
            </button>
          </div>
          <div class="keys-list">
            <div v-for="k in exportedKeys" :key="k" class="keys-list__row mono" @click="copyText(k)">
              {{ k }}
            </div>
          </div>
        </div>
      </div>
      <div class="modal__footer">
        <button class="btn btn--secondary btn--sm" @click="closeDialog('export')">Close</button>
        <button class="btn btn--primary btn--sm" @click="submitExport" :disabled="isExporting">
          {{ isExporting ? 'Exporting…' : 'Export' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.model-subtable {
  background: var(--bg-subtle);
  border-top: 1px solid var(--border);
}
.model-subtable .table thead th {
  background: transparent;
  font-size: var(--fs-xs);
}
.progress {
  height: 6px;
  background: var(--bg-muted);
  border-radius: var(--r-full);
  overflow: hidden;
}
.progress__bar {
  height: 100%;
  background: var(--accent);
  transition: width var(--dur-base) var(--ease-out);
}
.keys-list {
  max-height: 280px;
  overflow-y: auto;
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  background: var(--bg);
}
.keys-list__row {
  padding: var(--sp-2) var(--sp-3);
  font-size: var(--fs-sm);
  cursor: pointer;
  border-bottom: 1px solid var(--border-subtle);
  word-break: break-all;
  transition: background var(--dur-fast) var(--ease-out);
}
.keys-list__row:last-child { border-bottom: none; }
.keys-list__row:hover { background: var(--bg-subtle); }

.banner--success {
  background: var(--success-subtle);
  border-color: var(--success);
  color: var(--success-strong);
}
.banner--danger {
  background: var(--danger-subtle);
  border-color: var(--danger);
  color: var(--danger-strong);
}
</style>
