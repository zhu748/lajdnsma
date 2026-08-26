<script setup>
import { useDashboardStore } from '../../stores/dashboard'
import { ref, watch, nextTick, onMounted, computed } from 'vue'

const dashboardStore = useDashboardStore()
const currentFilter = ref('ALL')
const logContainer = ref(null)
const isFirstLoad = ref(true)
const userScrolled = ref(false)

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

function handleScroll() {
  userScrolled.value = true
}

const filteredLogs = computed(() => {
  if (currentFilter.value === 'ALL') return dashboardStore.logs
  return dashboardStore.logs.filter((l) => l.level === currentFilter.value)
})

const levelCounts = computed(() => {
  const counts = { INFO: 0, WARNING: 0, ERROR: 0 }
  dashboardStore.logs.forEach((l) => {
    if (counts[l.level] != null) counts[l.level]++
  })
  return counts
})

watch(
  () => dashboardStore.logs,
  async () => {
    await nextTick()
    if (isFirstLoad.value) {
      scrollToBottom()
      isFirstLoad.value = false
    } else if (isAtBottom()) {
      scrollToBottom()
    }
  },
  { deep: true }
)

watch(currentFilter, async () => {
  await nextTick()
  scrollToBottom()
})

onMounted(() => {
  if (dashboardStore.logs.length > 0) {
    nextTick(scrollToBottom)
  }
  if (logContainer.value) {
    logContainer.value.addEventListener('scroll', handleScroll)
  }
})
</script>

<template>
  <section class="section">
    <div class="section__header">
      <div class="section__title">Logs · {{ dashboardStore.logs.length }}</div>
      <div class="row">
        <button
          v-for="level in filters"
          :key="level"
          class="filter-chip"
          :class="{ 'filter-chip--active': currentFilter === level }"
          @click="setFilter(level)"
        >
          {{ level === 'ALL' ? 'All' : level === 'INFO' ? 'Info' : level === 'WARNING' ? 'Warn' : 'Error' }}
          <span v-if="level !== 'ALL'" class="filter-chip__count">{{ levelCounts[level] || 0 }}</span>
        </button>
      </div>
    </div>

    <div class="card">
      <div class="log-container" ref="logContainer">
        <div v-if="!filteredLogs.length" class="empty-state" style="padding:var(--sp-8);">
          <div class="empty-state__icon">∅</div>
          <div class="empty-state__title">No log entries</div>
          <div class="empty-state__desc">Logs will appear here as the service runs.</div>
        </div>
        <div
          v-for="(log, index) in filteredLogs"
          :key="index"
          class="log-entry"
          :class="'log-entry--' + (log.level || 'info').toLowerCase()"
        >
          <span class="log-entry__time mono">{{ log.timestamp }}</span>
          <span class="log-entry__level" :class="'log-entry__level--' + (log.level || 'info').toLowerCase()">
            {{ log.level || 'INFO' }}
          </span>
          <span class="log-entry__body">
            <template v-if="log.key !== 'N/A' && log.key">
              <span class="mono text-subtle">[{{ log.key }}]</span>
            </template>
            <template v-if="log.request_type !== 'N/A' && log.request_type">
              <span class="mono text-subtle">{{ log.request_type }}</span>
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
          </span>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
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
}
</style>
