<script setup>
import { useDashboardStore } from '../../../stores/dashboard'
import { computed } from 'vue'

const dashboardStore = useDashboardStore()

const localVersion = computed(() => dashboardStore.config.localVersion || '—')
const remoteVersion = computed(() => dashboardStore.config.remoteVersion || '—')
const hasUpdate = computed(() => dashboardStore.config.hasUpdate)
</script>

<template>
  <div class="card mt-4">
    <div class="card__header">
      <div class="card__title">Version</div>
      <span v-if="hasUpdate" class="pill pill--warning">
        <span class="pill__dot" />
        Update available
      </span>
      <span v-else-if="localVersion !== '—'" class="pill pill--success">
        <span class="pill__dot" />
        Up to date
      </span>
    </div>
    <div class="card__body">
      <div class="grid grid--2">
        <div class="kpi-mini">
          <div class="kpi-mini__label">Current</div>
          <div class="kpi-mini__value mono">{{ localVersion }}</div>
        </div>
        <div class="kpi-mini">
          <div class="kpi-mini__label">Latest</div>
          <div class="kpi-mini__value mono">{{ remoteVersion }}</div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.kpi-mini {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.kpi-mini__label {
  font-size: var(--fs-xs);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-muted);
  font-weight: var(--fw-medium);
}
.kpi-mini__value {
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
  color: var(--text-strong);
}
</style>
