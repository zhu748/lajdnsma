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
    addError.value = '请至少输入一个密钥'
    return
  }
  const keys = newApiKeys.value
    .split(/[,\n\r]+/)
    .map((s) => s.trim())
    .filter(Boolean)
  if (keys.some((k) => k.length < 10)) {
    addError.value = '部分密钥长度过短，请检查后重试'
    return
  }
  isAdding.value = true
  try {
    await dashboardStore.updateConfig('geminiApiKeys', newApiKeys.value, dashboardStore.sessionPassword)
    addSuccess.value = `已添加 ${keys.length} 个密钥`
    newApiKeys.value = ''
    await dashboardStore.fetchDashboardData()
    setTimeout(() => closeDialog('add'), 1500)
  } catch (e) {
    addError.value = e.message || '添加密钥失败'
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
    testError.value = '请输入密码'
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
    testError.value = e.message || '启动测试失败'
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
        testError.value = '进度轮询多次失败，测试可能已被中断'
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
      testError.value = '进度轮询多次失败，测试可能已被中断'
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
    clearError.value = '请输入密码'
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
    if (!r.ok) throw new Error(data.detail || '操作失败')
    clearSuccess.value = data.message || '已清除'
    clearPassword.value = ''
    await dashboardStore.fetchDashboardData()
    setTimeout(() => closeDialog('clear'), 1500)
  } catch (e) {
    clearError.value = e.message || '操作失败'
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
    exportError.value = '请输入密码'
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
    if (!r.ok) throw new Error(data.detail || '操作失败')
    exportedKeys.value = data.keys || []
    exportPassword.value = ''
  } catch (e) {
    exportError.value = e.message || '操作失败'
  } finally {
    isExporting.value = false
  }
}

// ---------- 复制反馈 ----------
const copyToast = ref('')
let copyToastTimer = null
onUnmounted(() => clearTimeout(copyToastTimer))

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text)
    copyToast.value = '已复制到剪贴板'
  } catch (e) {
    console.error('copy:', e)
    copyToast.value = '复制失败，请手动复制'
  }
  clearTimeout(copyToastTimer)
  copyToastTimer = setTimeout(() => (copyToast.value = ''), 1600)
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
        <div class="card__title">API 密钥</div>
        <div class="card__subtitle">
          {{ dashboardStore.apiKeyStats.length }} 个密钥 ·
          调用 {{ fmt(totalCalls) }} 次 · 令牌 {{ fmt(totalTokens) }}（24小时）
        </div>
      </div>
      <div class="row card__actions">
        <button class="btn btn--secondary btn--sm" @click="openDialog('add')">+ 添加</button>
        <button class="btn btn--secondary btn--sm" @click="openDialog('test')" :disabled="isTesting">
          {{ isTesting ? '测试中…' : '测试' }}
        </button>
        <button class="btn btn--secondary btn--sm" @click="openDialog('clear')">清除无效</button>
        <button class="btn btn--secondary btn--sm" @click="openDialog('export')">导出</button>
        <button class="btn btn--ghost btn--sm btn--icon" @click="showTable = !showTable" :title="showTable ? '收起' : '展开'">
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
              <th>密钥</th>
              <th style="text-align:right;">24h 调用</th>
              <th style="text-align:right;">令牌</th>
              <th>模型</th>
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
                    {{ modelList(stat).length }} 个模型 {{ collapsedKeys[stat.api_key] ? '▴' : '▾' }}
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
                          <th>模型</th>
                          <th style="text-align:right;">调用</th>
                          <th style="text-align:right;">令牌</th>
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
                  <div class="empty-state__title">密钥池为空</div>
                  <div class="empty-state__desc">点击「+ 添加」导入密钥。</div>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- ============== Pagination ============== -->
      <div v-if="totalPages > 1" class="row row--between" style="padding:var(--sp-3) var(--sp-5);border-top:1px solid var(--border);">
        <span class="text-muted" style="font-size:var(--fs-sm);">
          第 {{ currentPage }} / {{ totalPages }} 页
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
        <div class="modal__title">添加 API 密钥</div>
        <button class="btn btn--ghost btn--icon btn--sm" @click="closeDialog('add')">✕</button>
      </div>
      <div class="modal__body">
        <div class="field mb-4">
          <label class="field__label">密钥（每行一个或逗号分隔，支持 AIzaSy… 经典格式与 AQ.… 新格式）</label>
          <textarea v-model="newApiKeys" class="textarea" rows="6" placeholder="AIzaSy…&#10;AQ.…."></textarea>
        </div>
        <div v-if="addError" class="banner banner--danger">{{ addError }}</div>
        <div v-if="addSuccess" class="banner banner--success">{{ addSuccess }}</div>
      </div>
      <div class="modal__footer">
        <button class="btn btn--secondary btn--sm" @click="closeDialog('add')">取消</button>
        <button class="btn btn--primary btn--sm" @click="submitAdd" :disabled="isAdding">
          {{ isAdding ? '添加中…' : '添加密钥' }}
        </button>
      </div>
    </div>
  </div>

  <!-- ============== Test dialog ============== -->
  <div v-if="dialogState.test" class="modal-overlay" @click.self="!isTesting && closeDialog('test')">
    <div class="modal">
      <div class="modal__header">
        <div class="modal__title">测试密钥</div>
        <button class="btn btn--ghost btn--icon btn--sm" @click="closeDialog('test')" :disabled="isTesting">✕</button>
      </div>
      <div class="modal__body">
        <p class="text-muted mb-4" style="font-size:var(--fs-sm);">
          逐个探测密钥池中的密钥以验证可用性。此操作会消耗上游速率配额。
        </p>
        <div class="field mb-4">
          <label class="field__label">管理密码</label>
          <input v-model="testPassword" type="password" class="input" autocomplete="current-password">
        </div>
        <div v-if="testError" class="banner banner--danger">{{ testError }}</div>
        <div v-if="testProgress.total > 0" class="mt-4">
          <div class="row row--between mb-2" style="font-size:var(--fs-sm);">
            <span class="text-muted">进度</span>
            <span class="mono">{{ testProgress.completed }} / {{ testProgress.total }}</span>
          </div>
          <div class="progress">
            <div
              class="progress__bar"
              :style="{ width: ((testProgress.completed / Math.max(1, testProgress.total)) * 100) + '%' }"
            ></div>
          </div>
          <div class="row row--between mt-2" style="font-size:var(--fs-sm);">
            <span class="text-success">✓ {{ testProgress.valid }} 个有效</span>
            <span class="text-danger">✕ {{ testProgress.invalid }} 个无效</span>
          </div>
        </div>
      </div>
      <div class="modal__footer">
        <button class="btn btn--secondary btn--sm" @click="closeDialog('test')" :disabled="isTesting">取消</button>
        <button class="btn btn--primary btn--sm" @click="startTest" :disabled="isTesting">
          {{ isTesting ? '测试中…' : '开始测试' }}
        </button>
      </div>
    </div>
  </div>

  <!-- ============== Clear invalid dialog ============== -->
  <div v-if="dialogState.clear" class="modal-overlay" @click.self="closeDialog('clear')">
    <div class="modal">
      <div class="modal__header">
        <div class="modal__title">清除无效密钥</div>
        <button class="btn btn--ghost btn--icon btn--sm" @click="closeDialog('clear')">✕</button>
      </div>
      <div class="modal__body">
        <p class="text-muted mb-4" style="font-size:var(--fs-sm);">
          将删除所有被标记为无效的密钥，此操作不可撤销。
        </p>
        <div class="field">
          <label class="field__label">管理密码</label>
          <input v-model="clearPassword" type="password" class="input" autocomplete="current-password">
        </div>
        <div v-if="clearError" class="banner banner--danger mt-4">{{ clearError }}</div>
        <div v-if="clearSuccess" class="banner banner--success mt-4">{{ clearSuccess }}</div>
      </div>
      <div class="modal__footer">
        <button class="btn btn--secondary btn--sm" @click="closeDialog('clear')">取消</button>
        <button class="btn btn--primary btn--sm" @click="submitClear" :disabled="isClearing">
          {{ isClearing ? '清除中…' : '确认清除' }}
        </button>
      </div>
    </div>
  </div>

  <!-- ============== Export dialog ============== -->
  <div v-if="dialogState.export" class="modal-overlay" @click.self="closeDialog('export')">
    <div class="modal" style="max-width:560px;">
      <div class="modal__header">
        <div class="modal__title">导出有效密钥</div>
        <button class="btn btn--ghost btn--icon btn--sm" @click="closeDialog('export')">✕</button>
      </div>
      <div class="modal__body">
        <div class="field mb-4">
          <label class="field__label">管理密码</label>
          <input v-model="exportPassword" type="password" class="input" autocomplete="current-password">
        </div>
        <div v-if="exportError" class="banner banner--danger">{{ exportError }}</div>
        <div v-if="exportedKeys.length">
          <div class="row row--between mb-2">
            <span class="section__title">{{ exportedKeys.length }} 个有效密钥</span>
            <button class="btn btn--secondary btn--sm" @click="copyText(exportedKeys.join('\n'))">
              复制全部
            </button>
          </div>
          <div class="keys-list">
            <div v-for="k in exportedKeys" :key="k" class="keys-list__row mono" title="点击复制" @click="copyText(k)">
              {{ k }}
            </div>
          </div>
        </div>
      </div>
      <div class="modal__footer">
        <button class="btn btn--secondary btn--sm" @click="closeDialog('export')">关闭</button>
        <button class="btn btn--primary btn--sm" @click="submitExport" :disabled="isExporting">
          {{ isExporting ? '导出中…' : '导出' }}
        </button>
      </div>
    </div>
  </div>

  <!-- ============== Copy toast ============== -->
  <Transition name="toast-fade">
    <div v-if="copyToast" class="copy-toast">{{ copyToast }}</div>
  </Transition>
</template>

<style scoped>
/* 卡片操作按钮行：允许换行，窄屏不溢出 */
.card__actions {
  flex-wrap: wrap;
  justify-content: flex-end;
}

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

/* 复制反馈 toast：移动端居中显示 */
.copy-toast {
  position: fixed;
  left: 50%;
  bottom: calc(var(--sp-8) + env(safe-area-inset-bottom, 0px));
  transform: translateX(-50%);
  z-index: 200;
  padding: var(--sp-2) var(--sp-4);
  background: var(--bg-inverse);
  color: var(--text-inverse);
  border-radius: var(--r-full);
  font-size: var(--fs-sm);
  box-shadow: var(--shadow-lg);
  pointer-events: none;
  white-space: nowrap;
}
.toast-fade-enter-active,
.toast-fade-leave-active {
  transition: opacity var(--dur-base) var(--ease-out), transform var(--dur-base) var(--ease-out);
}
.toast-fade-enter-from,
.toast-fade-leave-to {
  opacity: 0;
  transform: translateX(-50%) translateY(8px);
}

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
