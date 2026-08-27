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

// 最后更新时间（每秒刷新显示，让「x 秒前」自然跳动）
const nowTick = ref(Date.now())
let tickTimer = null
onMounted(() => {
  tickTimer = setInterval(() => (nowTick.value = Date.now()), 1000)
})
onUnmounted(() => clearInterval(tickTimer))

const lastUpdatedText = computed(() => {
  const t = dashboardStore.lastUpdated
  if (!t) return ''
  const diff = Math.max(0, Math.floor((nowTick.value - t.getTime()) / 1000))
  if (diff < 5) return '刚刚更新'
  if (diff < 60) return `${diff} 秒前更新`
  return `更新于 ${t.toLocaleTimeString('zh-CN', { hour12: false })}`
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
        <!-- 4 KPI 卡片 — 图标 + 左侧彩色小色条 + 用量徽标，运维人员
             不读文字也能快速扫出各项状态与水位。 -->
        <div class="kpi kpi--info">
          <span class="kpi__accent kpi__accent--info" aria-hidden="true"></span>
          <div class="kpi__head">
            <span class="kpi__icon kpi__icon--info" aria-hidden="true">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4" />
              </svg>
            </span>
            <div class="kpi__label">密钥数</div>
          </div>
          <div class="kpi__value">{{ dashboardStore.status.keyCount }}</div>
          <div class="kpi__hint">池内有效</div>
        </div>
        <div class="kpi kpi--clickable kpi--accent-2" @click="showModelsModal = true">
          <span class="kpi__accent kpi__accent--accent" aria-hidden="true"></span>
          <div class="kpi__head">
            <span class="kpi__icon kpi__icon--accent" aria-hidden="true">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="3" />
                <path d="M12 2v4m0 12v4M4.93 4.93l2.83 2.83m8.48 8.48l2.83 2.83M2 12h4m12 0h4M4.93 19.07l2.83-2.83m8.48-8.48l2.83-2.83" />
              </svg>
            </span>
            <div class="kpi__label">模型</div>
          </div>
          <div class="kpi__value">{{ dashboardStore.status.modelCount }}</div>
          <div class="kpi__hint">点击查看列表 →</div>
        </div>
        <div class="kpi kpi--success">
          <span class="kpi__accent kpi__accent--success" aria-hidden="true"></span>
          <div class="kpi__head">
            <span class="kpi__icon kpi__icon--success" aria-hidden="true">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
              </svg>
            </span>
            <div class="kpi__label">调用 · 24小时</div>
          </div>
          <div class="kpi__value">{{ fmt(dashboardStore.status.last24hCalls) }}</div>
          <div class="kpi__hint">每小时 {{ fmt(dashboardStore.status.hourlyCalls) }} · 每分钟 {{ fmt(dashboardStore.status.minuteCalls) }}</div>
        </div>
        <div class="kpi kpi--warning">
          <span class="kpi__accent kpi__accent--warning" aria-hidden="true"></span>
          <div class="kpi__head">
            <span class="kpi__icon kpi__icon--warning" aria-hidden="true">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M6 2h12M6 22h12" />
                <rect x="7" y="2" width="10" height="20" rx="2" />
                <path d="M10 6h4" />
              </svg>
            </span>
            <div class="kpi__label">令牌 · 24小时</div>
          </div>
          <div class="kpi__value">{{ fmt(dashboardStore.status.last24hTokens) }}</div>
          <div class="kpi__hint">每小时 {{ fmt(dashboardStore.status.hourlyTokens) }} · 每分钟 {{ fmt(dashboardStore.status.minuteTokens) }}</div>
        </div>
      </div>

      <!-- Vertex mode info -->
      <div v-else class="vertex-info">
        <div class="vertex-info__icon">ℹ</div>
        <div class="vertex-info__body">
          <div class="vertex-info__title">Vertex 模式已启用</div>
          <div class="vertex-info__text">
            当前服务正通过 Vertex AI 后端路由。该模式下，统计信息与各密钥计数将单独上报。
          </div>
        </div>
      </div>

      <!-- 数据新鲜度指示器：显示距离上次成功拉取的时间，
           服务离线/后端卡顿时运维能第一时间发现数据不新鲜 -->
      <div v-if="!dashboardStore.status.enableVertex && lastUpdatedText" class="freshness-bar">
        <span class="freshness-bar__dot" :class="{ 'freshness-bar__dot--stale': dashboardStore.isRefreshing }"></span>
        <span class="freshness-bar__text">{{ lastUpdatedText }}</span>
      </div>
    </div>

    <!-- Models modal -->
    <div v-if="showModelsModal" class="modal-overlay" @click.self="showModelsModal = false">
      <div class="modal" style="max-width:560px;">
        <div class="modal__header">
          <div>
            <div class="modal__title">可用模型</div>
            <div class="card__subtitle">共 {{ modelsList.total }} 个</div>
          </div>
          <button class="btn btn--ghost btn--icon btn--sm" @click="showModelsModal = false">✕</button>
        </div>
        <div class="modal__body" style="max-height:60vh;overflow-y:auto;">
          <div v-if="modelsList.standard.length" class="mb-4">
            <div class="section__title mb-2">标准模型</div>
            <div class="chip-grid">
              <span v-for="m in modelsList.standard" :key="m" class="chip mono">{{ m }}</span>
            </div>
          </div>
          <div v-if="modelsList.search.length">
            <div class="section__title mb-2">搜索模型</div>
            <div class="chip-grid">
              <span v-for="m in modelsList.search" :key="m" class="chip chip--accent mono">{{ m }}</span>
            </div>
          </div>
          <div v-if="!modelsList.total" class="empty-state">
            <div class="empty-state__icon">∅</div>
            <div class="empty-state__title">未加载模型</div>
            <div class="empty-state__desc">请检查 API 密钥配置。</div>
          </div>
        </div>
        <div class="modal__footer">
          <button class="btn btn--secondary btn--sm" @click="showModelsModal = false">关闭</button>
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

/* Round icon chip at the top of each KPI card — 图标与色条同色系，
   扫视时先看图标形状、再看数字 */
.kpi__head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.kpi__icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border-radius: var(--r-md);
  flex-shrink: 0;
}
.kpi__icon--info {
  background: var(--info-subtle);
  color: var(--info-strong);
}
.kpi__icon--accent {
  background: var(--accent-subtle);
  color: var(--accent-strong);
}
.kpi__icon--success {
  background: var(--success-subtle);
  color: var(--success-strong);
}
.kpi__icon--warning {
  background: var(--warning-subtle);
  color: var(--warning-strong);
}

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

/* 中文提示在小屏可能换行，需保证行高舒展 */
@media (max-width: 640px) {
  .kpi__hint {
    line-height: var(--lh-base);
  }
}

.kpi--clickable {
  cursor: pointer;
  transition: background var(--dur-fast) var(--ease-out);
}
.kpi--clickable:hover {
  background: var(--bg-subtle);
}

/* 数据新鲜度指示条：贴在 KPI 网格底部的细条 */
.freshness-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px var(--sp-5);
  border-top: 1px solid var(--border-subtle);
  font-size: var(--fs-xs);
  color: var(--text-subtle);
}
.freshness-bar__dot {
  width: 6px;
  height: 6px;
  border-radius: var(--r-full);
  background: var(--success);
  animation: freshness-pulse 2.4s var(--ease-out) infinite;
}
.freshness-bar__dot--stale {
  background: var(--warning);
}
@keyframes freshness-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.35; }
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
