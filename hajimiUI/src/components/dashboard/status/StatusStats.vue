<script setup>
import { computed, ref, onMounted, onUnmounted } from 'vue'
import { useDashboardStore } from '../../../stores/dashboard'

const dashboardStore = useDashboardStore()
const showModelsModal = ref(false)

const modelsList = computed(() => {
  try {
    const models = dashboardStore.availableModels || []
    const standardModels = []
    const searchModels = []
    models.forEach((m) => {
      if (m === 'all') return
      const formatted = m.replace('models/', '')
      if (m.endsWith('-search')) searchModels.push(formatted)
      else standardModels.push(formatted)
    })
    return {
      standard: standardModels,
      search: searchModels,
      total: standardModels.length + searchModels.length,
    }
  } catch {
    return { standard: [], search: [], total: 0 }
  }
})

const handleEscKey = (e) => {
  if (e.key === 'Escape' && showModelsModal.value) showModelsModal.value = false
}
onMounted(() => document.addEventListener('keydown', handleEscKey))
onUnmounted(() => document.removeEventListener('keydown', handleEscKey))

function fmt(n) {
  if (n == null) return '0'
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B'
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M'
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k'
  return String(n)
}
</script>

<template>
  <div class="card">
    <div class="card__body" style="padding:0;">
      <div class="stats-grid" v-if="!dashboardStore.status.enableVertex">
        <!-- 4 KPI cards — each card carries a small accent strip on the
             left edge so operators can scan status at a glance without
             reading the labels. -->
        <div class="kpi kpi--info">
          <span class="kpi__accent kpi__accent--info" aria-hidden="true"></span>
          <div class="kpi__label">Keys</div>
          <div class="kpi__value">{{ dashboardStore.status.keyCount }}</div>
          <div class="kpi__hint">valid in pool</div>
        </div>
        <div class="kpi kpi--clickable kpi--accent-2" @click="showModelsModal = true">
          <span class="kpi__accent kpi__accent--accent" aria-hidden="true"></span>
          <div class="kpi__label">Models</div>
          <div class="kpi__value">{{ dashboardStore.status.modelCount }}</div>
          <div class="kpi__hint">click to view list →</div>
        </div>
        <div class="kpi kpi--success">
          <span class="kpi__accent kpi__accent--success" aria-hidden="true"></span>
          <div class="kpi__label">Calls · 24h</div>
          <div class="kpi__value">{{ fmt(dashboardStore.status.last24hCalls) }}</div>
          <div class="kpi__hint">{{ fmt(dashboardStore.status.hourlyCalls) }} / hour · {{ fmt(dashboardStore.status.minuteCalls) }} / min</div>
        </div>
        <div class="kpi kpi--warning">
          <span class="kpi__accent kpi__accent--warning" aria-hidden="true"></span>
          <div class="kpi__label">Tokens · 24h</div>
          <div class="kpi__value">{{ fmt(dashboardStore.status.last24hTokens) }}</div>
          <div class="kpi__hint">{{ fmt(dashboardStore.status.hourlyTokens) }} / hour · {{ fmt(dashboardStore.status.minuteTokens) }} / min</div>
        </div>
      </div>

      <!-- Vertex mode info -->
      <div v-else class="vertex-info">
        <div class="vertex-info__icon">ℹ</div>
        <div class="vertex-info__body">
          <div class="vertex-info__title">Vertex mode is active</div>
          <div class="vertex-info__text">
            The service is currently routing through the Vertex AI backend. Stats and per-key counters are reported separately in that mode.
          </div>
        </div>
      </div>
    </div>

    <!-- Models modal -->
    <div v-if="showModelsModal" class="modal-overlay" @click.self="showModelsModal = false">
      <div class="modal" style="max-width:560px;">
        <div class="modal__header">
          <div>
            <div class="modal__title">Available models</div>
            <div class="card__subtitle">{{ modelsList.total }} total</div>
          </div>
          <button class="btn btn--ghost btn--icon btn--sm" @click="showModelsModal = false">✕</button>
        </div>
        <div class="modal__body" style="max-height:60vh;overflow-y:auto;">
          <div v-if="modelsList.standard.length" class="mb-4">
            <div class="section__title mb-2">Standard</div>
            <div class="chip-grid">
              <span v-for="m in modelsList.standard" :key="m" class="chip mono">{{ m }}</span>
            </div>
          </div>
          <div v-if="modelsList.search.length">
            <div class="section__title mb-2">Search</div>
            <div class="chip-grid">
              <span v-for="m in modelsList.search" :key="m" class="chip chip--accent mono">{{ m }}</span>
            </div>
          </div>
          <div v-if="!modelsList.total" class="empty-state">
            <div class="empty-state__icon">∅</div>
            <div class="empty-state__title">No models loaded</div>
            <div class="empty-state__desc">Check the API key configuration.</div>
          </div>
        </div>
        <div class="modal__footer">
          <button class="btn btn--secondary btn--sm" @click="showModelsModal = false">Close</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.stats-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 0;
}
@media (max-width: 1024px) {
  .stats-grid { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width: 640px) {
  .stats-grid { grid-template-columns: 1fr; }
}

.kpi {
  position: relative;
  padding: var(--sp-4) var(--sp-5) var(--sp-4) calc(var(--sp-5) + 4px);
  border-right: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  gap: 4px;
  overflow: hidden;
}
.kpi::before {
  /* Subtle top accent bar that becomes visible on hover to
     signal interactivity without making the dashboard visually
     noisy at rest. */
  content: '';
  position: absolute;
  inset: 0 0 auto 0;
  height: 2px;
  background: transparent;
  transition: background var(--dur-base) var(--ease-out);
}
.kpi:hover::before {
  background: var(--border-strong);
}
.kpi:nth-child(4n) { border-right: none; }
.kpi:nth-last-child(-n+2) { border-bottom: none; }
@media (max-width: 1024px) {
  .kpi:nth-child(2n) { border-right: none; }
  .kpi:nth-last-child(-n+2) { border-bottom: 1px solid var(--border); }
  .kpi:nth-last-child(-n+1) { border-bottom: none; }
}
@media (max-width: 640px) {
  .kpi { border-right: none; }
  .kpi:nth-last-child(-n+2) { border-bottom: 1px solid var(--border); }
  .kpi:last-child { border-bottom: none; }
}

/* Per-card colored left accent strip — adds visual hierarchy
   without overwhelming the layout. */
.kpi__accent {
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 3px;
  border-radius: 0;
  opacity: 0.65;
  transition: opacity var(--dur-base) var(--ease-out);
}
.kpi:hover .kpi__accent { opacity: 1; }
.kpi__accent--info { background: var(--info); }
.kpi__accent--accent { background: var(--accent); }
.kpi__accent--success { background: var(--success); }
.kpi__accent--warning { background: var(--warning); }

.kpi__label {
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-muted);
}
.kpi__value {
  font-size: 28px;
  font-weight: var(--fw-semibold);
  color: var(--text-strong);
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
  line-height: 1.1;
  margin-top: 4px;
}
.kpi__hint {
  font-size: var(--fs-xs);
  color: var(--text-subtle);
  margin-top: 4px;
}

.kpi--clickable {
  cursor: pointer;
  transition: background var(--dur-fast) var(--ease-out);
}
.kpi--clickable:hover {
  background: var(--bg-subtle);
}

.vertex-info {
  display: flex;
  gap: var(--sp-4);
  padding: var(--sp-5);
  background: var(--info-subtle);
  border-left: 3px solid var(--info);
  border-radius: var(--r-md);
  margin: var(--sp-3);
}
.vertex-info__icon {
  width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--info);
  color: white;
  border-radius: var(--r-full);
  font-weight: var(--fw-bold);
  font-size: 14px;
  flex-shrink: 0;
}
.vertex-info__title {
  font-size: var(--fs-md);
  font-weight: var(--fw-semibold);
  color: var(--text-strong);
  margin-bottom: 4px;
}
.vertex-info__text {
  font-size: var(--fs-sm);
  color: var(--text-muted);
  line-height: var(--lh-base);
}

.chip-grid {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-1);
}
.chip {
  display: inline-flex;
  align-items: center;
  padding: 4px 8px;
  background: var(--bg-subtle);
  border: 1px solid var(--border);
  border-radius: var(--r-sm);
  font-size: var(--fs-xs);
  color: var(--text);
}
.chip--accent {
  background: var(--accent-subtle);
  border-color: var(--accent);
  color: var(--accent-strong);
}
</style>
