<script setup>
import { useDashboardStore } from '../../../stores/dashboard'
import { ref, reactive, watch } from 'vue'

const dashboardStore = useDashboardStore()

const localConfig = reactive({
  maxRequestsPerMinute: 0,
  maxRequestsPerDayPerIp: 0,
  keyRotationStrategy: 'fill',
})

const populatedFromStore = ref(false)

watch(
  () => ({
    a: dashboardStore.config.maxRequestsPerMinute,
    b: dashboardStore.config.maxRequestsPerDayPerIp,
    c: dashboardStore.config.keyRotationStrategy,
    loaded: dashboardStore.isConfigLoaded,
  }),
  (n) => {
    if (n.loaded && !populatedFromStore.value) {
      localConfig.maxRequestsPerMinute = n.a
      localConfig.maxRequestsPerDayPerIp = n.b
      localConfig.keyRotationStrategy = n.c || 'fill'
      populatedFromStore.value = true
    }
  },
  { deep: true, immediate: true }
)

async function saveComponentConfigs(passwordFromParent) {
  if (!passwordFromParent) return { success: false, message: 'Basic: password missing' }
  let allSucceeded = true
  const messages = []
  for (const key of Object.keys(localConfig)) {
    if (localConfig[key] !== dashboardStore.config[key]) {
      try {
        await dashboardStore.updateConfig(key, localConfig[key], passwordFromParent)
        dashboardStore.config[key] = localConfig[key]
        messages.push(`${key} ok`)
      } catch (e) {
        allSucceeded = false
        messages.push(`${key} fail: ${e.message}`)
      }
    }
  }
  if (allSucceeded && !messages.length) return { success: true, message: 'Basic: no changes' }
  return { success: allSucceeded, message: `Basic: ${messages.join('; ')}` }
}

defineExpose({ saveComponentConfigs, localConfig })
</script>

<template>
  <div class="sub-section">
    <div class="sub-section__title">Basic</div>
    <div class="grid grid--2">
      <div class="field">
        <label class="field__label">RPM limit</label>
        <input
          v-model.number="localConfig.maxRequestsPerMinute"
          type="number"
          min="0"
          class="input"
        >
        <div class="field__hint">Max inbound requests per IP per minute.</div>
      </div>
      <div class="field">
        <label class="field__label">Daily per-IP limit</label>
        <input
          v-model.number="localConfig.maxRequestsPerDayPerIp"
          type="number"
          min="0"
          class="input"
        >
        <div class="field__hint">Max inbound requests per IP per day.</div>
      </div>
    </div>

    <div class="field">
      <label class="field__label">Key rotation strategy</label>
      <div class="radio-row">
        <label class="radio-card" :class="{ 'radio-card--active': localConfig.keyRotationStrategy === 'fill' }">
          <input
            type="radio"
            value="fill"
            v-model="localConfig.keyRotationStrategy"
            name="keyRotationStrategy"
          >
          <span class="radio-card__title">Fill (sticky, default)</span>
          <span class="radio-card__desc">
            Keep using the same key until it hits quota/cooldown, then advance.
            Produces a single-user RPM signature that is harder for Google risk
            control to flag as a key pool.
          </span>
        </label>
        <label class="radio-card" :class="{ 'radio-card--active': localConfig.keyRotationStrategy === 'polling' }">
          <input
            type="radio"
            value="polling"
            v-model="localConfig.keyRotationStrategy"
            name="keyRotationStrategy"
          >
          <span class="radio-card__title">Polling (round-robin)</span>
          <span class="radio-card__desc">
            Original behaviour — rotate to a different key on every request.
            Better for explicit per-key load balancing but creates a visible
            multi-key fingerprint.
          </span>
        </label>
      </div>
      <div class="field__hint">
        Switch takes effect immediately and resets the key stack so the next
        request picks up the new strategy.
      </div>
    </div>
  </div>
</template>

<style scoped>
.sub-section {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}
.sub-section__title {
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-muted);
}

.radio-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--sp-3);
}

@media (max-width: 720px) {
  .radio-row {
    grid-template-columns: 1fr;
  }
}

.radio-card {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  padding: var(--sp-3);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  background: var(--surface-2);
  cursor: pointer;
  transition: border-color 0.18s ease, background 0.18s ease;
  position: relative;
}

.radio-card input[type='radio'] {
  position: absolute;
  top: var(--sp-2);
  right: var(--sp-2);
  width: 16px;
  height: 16px;
  accent-color: var(--accent);
}

.radio-card--active {
  border-color: var(--accent);
  background: var(--surface-3);
}

.radio-card__title {
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  color: var(--text-strong);
  padding-right: 28px;
}

.radio-card__desc {
  font-size: var(--fs-xs);
  line-height: 1.5;
  color: var(--text-muted);
}
</style>
