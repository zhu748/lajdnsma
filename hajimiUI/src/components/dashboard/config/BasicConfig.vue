<script setup>
import { useDashboardStore } from '../../../stores/dashboard'
import { ref, reactive, watch } from 'vue'

const dashboardStore = useDashboardStore()

const localConfig = reactive({
  maxRequestsPerMinute: 0,
  maxRequestsPerDayPerIp: 0,
})

const populatedFromStore = ref(false)

watch(
  () => ({
    a: dashboardStore.config.maxRequestsPerMinute,
    b: dashboardStore.config.maxRequestsPerDayPerIp,
    loaded: dashboardStore.isConfigLoaded,
  }),
  (n) => {
    if (n.loaded && !populatedFromStore.value) {
      localConfig.maxRequestsPerMinute = n.a
      localConfig.maxRequestsPerDayPerIp = n.b
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
</style>
