<script setup>
import { useDashboardStore } from '../../stores/dashboard'
import { ref, watch, nextTick, onMounted, onUnmounted, computed } from 'vue'

const dashboardStore = useDashboardStore()
const currentFilter = ref('ALL')
const query = ref('')
const paused = ref(false)
const frozenLogs = ref([])
const unseen = ref(0)
const logContainer = ref(null)
const isFirstLoad = ref(true)
// 自动滚动开关：翻看历史日志时自动暂停跟随，回到底部自动恢复
const autoScroll = ref(true)

const filterLabels = {
  ALL: '全部',
  INFO: '信息',
  WARNING: '警告',
  ERROR: '错误',
}

const filters = ['ALL', 'INFO', 'WARNING', 'ERROR']

function setFilter(level) {
  currentFilter.value = level
}

function isAtBottom() {
  if (!logContainer.value) return false
  const c = logContainer.value
  return c.scrollHeight - c.scrollTop - c.clientHeight < 50
}

function scrollToBottom() {
  if (logContainer.value) {
    logContainer.value.scrollTop = logContainer.value.scrollHeight
  }
}

const visibleLogs = computed(() => paused.value ? frozenLogs.value : dashboardStore.logs)
const filteredLogs = computed(() => visibleLogs.value.filter(l =>
  (currentFilter.value === 'ALL' || l.level === currentFilter.value) &&
  (!query.value || [l.request_id, l.attempt_id, l.model, l.message, l.path, l.error_message].join(' ').toLowerCase().includes(query.value.toLowerCase()))))
function togglePause() {
  if (!paused.value) frozenLogs.value = [...dashboardStore.logs]
  paused.value = !paused.value
  if (!paused.value) { unseen.value = 0; autoScroll.value = true; nextTick(scrollToBottom) }
}

const levelCounts = computed(() => {
  const counts = { INFO: 0, WARNING: 0, ERROR: 0 }
  dashboardStore.logs.forEach((l) => {
    if (counts[l.level] != null) counts[l.level]++
  })
  return counts
})

// 导出为纯文本：一行一条，带时间戳/级别/上下文，便于粘贴到 issue
function logsToText() {
  return filteredLogs.value
    .map((l) => {
      if (l.formatted) return l.formatted
      const ctx = [l.key, l.request_type_label || l.request_type, l.model, l.status_code]
        .filter((x) => x && x !== 'N/A')
        .map((x) => `[${x}]`)
        .join('')
      const err = l.error_message ? ` - ${l.error_message}` : ''
      return `${l.timestamp} [${l.level_label || filterLabels[l.level] || l.level}]${ctx} ${l.message}${err}`
    })
    .join('\n')
}

async function copyLogs() {
  const text = logsToText()
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    copyToast.value = `已复制 ${filteredLogs.value.length} 条日志`
  } catch (e) {
    console.error('copy logs:', e)
    copyToast.value = '复制失败，请改用下载'
  }
  showToast()
}

function downloadLogs() {
  const text = logsToText()
  if (!text) return
  const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  const ts = new Date().toISOString().slice(0, 19).replace(/[T:]/g, '-')
  a.href = url
  a.download = `gateway-logs-${ts}.txt`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
  copyToast.value = '日志已下载'
  showToast()
}

const copyToast = ref('')
let copyToastTimer = null
function showToast() {
  clearTimeout(copyToastTimer)
  copyToastTimer = setTimeout(() => (copyToast.value = ''), 1600)
}
onUnmounted(() => clearTimeout(copyToastTimer))

watch(
  () => dashboardStore.logs,
  async (next, previous = []) => {
    const ids = new Set(previous.map(l => l.log_id || l.created_ns || l.message))
    const added = next.filter(l => !ids.has(l.log_id || l.created_ns || l.message)).length
    const follow = autoScroll.value && !paused.value
    if (paused.value || !autoScroll.value) unseen.value += added
    await nextTick()
    if (isFirstLoad.value) {
      scrollToBottom()
      isFirstLoad.value = false
    } else if (follow) {
      scrollToBottom()
    }
  }
  , { flush: 'pre' }
  // Perf(round4): 移除 deep: true —— store 每次轮询都是 logs.value = data.logs
  // 整体替换引用，浅层 watch 已能触发回调；deep 模式还要递归遍历数百条
  // 日志对象的全部字段（每 5s 一次），属于纯浪费。若未来变为原地修改
  // 数组元素，才需要重新加回 deep。
)

watch(currentFilter, async () => {
  await nextTick()
  scrollToBottom()
})

// 用户主动滚动 = 想看历史：只要离开底部就自动暂停跟随，
// 不需要手动关开关；滚回底部自动恢复。
function handleScroll() {
  if (isFirstLoad.value) return
  autoScroll.value = isAtBottom()
}

onMounted(() => {
  if (dashboardStore.logs.length > 0) {
    nextTick(scrollToBottom)
  }
})
</script>

<template>
  <section class="section">
    <div class="section__header">
      <div class="section__title">系统日志 · {{ dashboardStore.logs.length }}</div>
      <div class="row log-toolbar">
        <input class="input log-search" v-model="query" placeholder="搜索编号、模型或内容" aria-label="搜索系统日志">
        <div class="row log-filters">
          <button
            v-for="level in filters"
            :key="level"
            class="filter-chip"
            :class="{ 'filter-chip--active': currentFilter === level }"
            :aria-pressed="currentFilter === level"
            @click="setFilter(level)"
          >
            {{ filterLabels[level] || level }}
            <span v-if="level !== 'ALL'" class="filter-chip__count">{{ levelCounts[level] || 0 }}</span>
          </button>
        </div>
        <div class="row log-actions">
          <button class="btn btn--ghost btn--sm" @click="togglePause" :aria-pressed="paused">{{ paused ? '恢复实时' : '暂停更新' }}</button>
          <button
            class="btn btn--ghost btn--sm log-action-btn"
            title="滚动到底部并恢复自动跟随"
            @click="autoScroll = true; unseen = 0; nextTick(scrollToBottom)"
          >
            ↓ {{ unseen ? `${unseen} 条新记录 · ` : '' }}跟随
          </button>
          <button
            class="btn btn--ghost btn--sm log-action-btn"
            title="复制当前筛选下的全部日志"
            :disabled="!filteredLogs.length"
            @click="copyLogs"
          >
            复制
          </button>
          <button
            class="btn btn--ghost btn--sm log-action-btn"
            title="下载为 .txt 文件"
            :disabled="!filteredLogs.length"
            @click="downloadLogs"
          >
            下载
          </button>
        </div>
      </div>
    </div>

    <p class="section-description">仅保留当前进程最近 {{ dashboardStore.logRetention.capacity }} 条日志；复制和下载包含当前筛选的保留内容。{{ paused ? '已暂停更新。' : '' }}</p>
    <p v-if="dashboardStore.logRetention.gap" role="status" class="text-muted">服务重启或缓冲区淘汰造成历史缺口，完整记录请查看服务端输出。</p>
    <div class="card">
      <div class="log-container" ref="logContainer" @scroll.passive="handleScroll">
        <div v-if="!filteredLogs.length" class="empty-state" style="padding:var(--sp-8);">
          <div class="empty-state__icon">∅</div>
          <div class="empty-state__title">暂无日志</div>
          <div class="empty-state__desc">服务运行时日志将显示在这里。</div>
        </div>
        <div
          v-for="(log, index) in filteredLogs"
          :key="log.log_id || log.created_ns || `${log.timestamp}-${log.message}-${index}`"
          class="log-entry"
          :class="'log-entry--' + (log.level || 'info').toLowerCase()"
        >
          <span class="log-entry__time mono">{{ log.timestamp }}</span>
          <span class="log-entry__level" :class="'log-entry__level--' + (log.level || 'info').toLowerCase()">
            {{ log.level_label || filterLabels[log.level] || log.level || '信息' }}
          </span>
          <span class="log-entry__body">
            <span v-if="log.request_id" class="mono text-subtle">#{{ log.request_id }} {{ log.attempt_id }}</span>
            <template v-if="log.key !== 'N/A' && log.key">
              <span class="mono text-subtle">[{{ log.key }}]</span>
            </template>
            <template v-if="log.request_type !== 'N/A' && log.request_type">
              <span class="mono text-subtle">{{ log.request_type_label || log.request_type }}</span>
            </template>
            <template v-if="log.model !== 'N/A' && log.model">
              <span class="mono text-subtle">[{{ log.model }}]</span>
            </template>
            <template v-if="log.status_code !== 'N/A' && log.status_code">
              <span class="mono text-subtle">{{ log.status_code }}</span>
            </template>
            <span class="log-entry__msg">{{ log.message }}</span>
            <template v-if="log.error_message">
              <span class="log-entry__err"> — {{ log.error_message }}</span>
            </template>
            <details v-if="log.event === 'request_completed'" class="log-result">
              <summary>查看返回与思考摘要 · 请求 {{ log.request_id }}</summary>
              <div class="text-subtle">{{ log.path }} · {{ log.source }} · {{ log.outcome }}</div>
              <template v-if="log.response_preview">
                <div>返回预览（共 {{ log.response_chars }} 字）</div>
                <pre>{{ log.response_preview }}</pre>
              </template>
              <template v-if="log.reasoning_preview">
                <div>思考预览（共 {{ log.reasoning_chars }} 字）</div>
                <pre>{{ log.reasoning_preview }}</pre>
              </template>
              <div v-if="!log.response_preview && !log.reasoning_preview" class="text-subtle">未返回可展示的文本，或已关闭内容预览。</div>
              <div v-if="log.capture_truncated" class="text-subtle">响应较大，仅记录有限长度的摘要。</div>
            </details>
          </span>
        </div>
      </div>
    </div>

    <!-- 复制/下载反馈 toast -->
    <Transition name="toast-fade">
      <div v-if="copyToast" class="copy-toast">{{ copyToast }}</div>
    </Transition>
  </section>
</template>

<style scoped>
/* 日志工具行：筛选 chips + 复制/下载按钮，窄屏允许换行 */
.log-search { max-width: 260px; height: 30px; }
.log-toolbar {
  flex-wrap: wrap;
  gap: var(--sp-2);
}
.log-filters {
  flex-wrap: wrap;
}
.log-actions {
  gap: 2px;
}
.log-action-btn {
  height: 24px;
  padding: 0 8px;
  font-size: var(--fs-xs);
}

.filter-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 24px;
  padding: 0 10px;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--r-full);
  color: var(--text-muted);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  transition: all var(--dur-fast) var(--ease-out);
  cursor: pointer;
}

/* 移动端：筛选 chip 加大触控高度 */
@media (max-width: 768px) {
  .filter-chip {
    height: 30px;
    padding: 0 12px;
    font-size: var(--fs-sm);
  }
  .log-action-btn {
    height: 30px;
    padding: 0 10px;
    font-size: var(--fs-sm);
  }
}
.filter-chip:hover {
  background: var(--bg-subtle);
  color: var(--text);
}
.filter-chip--active {
  background: var(--accent);
  border-color: var(--accent);
  color: white;
}
.filter-chip__count {
  padding: 0 6px;
  background: rgba(0, 0, 0, 0.08);
  border-radius: var(--r-full);
  font-size: 10px;
  font-variant-numeric: tabular-nums;
}
.filter-chip--active .filter-chip__count {
  background: rgba(255, 255, 255, 0.2);
}

.log-container {
  max-height: 480px;
  overflow-y: auto;
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  line-height: var(--lh-base);
  background: var(--bg);
  /* 细滚动条：日志面板高频滚动，粗滚动条浪费横向空间 */
  scrollbar-width: thin;
  overscroll-behavior: contain;
}

.log-entry {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-2);
  padding: 6px var(--sp-4);
  border-bottom: 1px solid var(--border-subtle);
  transition: background var(--dur-fast) var(--ease-out);
}
.log-entry:hover {
  background: var(--bg-subtle);
}
.log-entry:last-child {
  border-bottom: none;
}

.log-entry__time {
  color: var(--text-subtle);
  flex-shrink: 0;
  width: 152px;
}

.log-entry__level {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 50px;
  height: 18px;
  padding: 0 6px;
  border-radius: var(--r-sm);
  font-size: 10px;
  font-weight: var(--fw-semibold);
  letter-spacing: 0.04em;
  flex-shrink: 0;
  text-transform: uppercase;
}
.log-entry__level--info {
  background: var(--info-subtle);
  color: var(--info-strong);
}
.log-entry__level--warning {
  background: var(--warning-subtle);
  color: var(--warning-strong);
}
.log-entry__level--error {
  background: var(--danger-subtle);
  color: var(--danger-strong);
}

.log-entry__body {
  flex: 1;
  color: var(--text);
  word-break: break-word;
  /* Subtle inline metadata first, then the message — visually the
     message dominates and the metadata acts as a quiet prefix. */
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 6px;
}
.log-entry__msg {
  color: var(--text);
  /* Slightly brighter than the meta prefix so the message body
     draws the eye. */
  font-weight: var(--fw-medium);
}
.log-entry__err {
  color: var(--danger-strong);
  font-weight: var(--fw-regular);
}
.log-result {
  flex-basis: 100%;
  min-width: 0;
  margin-top: 4px;
}
.log-result summary {
  cursor: pointer;
  color: var(--text-muted);
}
.log-result pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 160px;
  overflow-y: auto;
  margin: 4px 0 8px;
  padding: 8px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  background: var(--bg-elevated);
  font: inherit;
}

/* Subtle left-side accent on error/warning entries so operators can
   spot problems at a glance without scanning the level pill. */
.log-entry--error {
  border-left: 2px solid var(--danger);
  padding-left: calc(var(--sp-4) - 2px);
  background: var(--danger-subtle);
}
.log-entry--warning {
  border-left: 2px solid var(--warning);
  padding-left: calc(var(--sp-4) - 2px);
}
.log-entry--error:hover {
  background: color-mix(in srgb, var(--danger-subtle) 70%, var(--bg-subtle));
}

@media (max-width: 768px) {
  .log-entry {
    flex-wrap: wrap;
    padding: 6px var(--sp-3);
  }
  .log-entry__time {
    width: 100%;
  }
  .log-container {
    max-height: 360px;
    font-size: 11px;
  }
}

/* 复制反馈 toast */
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
</style>
