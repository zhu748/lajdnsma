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
  if (!passwordFromParent) return { success: false, message: '基础配置：缺少密码' }
  let allSucceeded = true
  const messages = []
  const labelMap = {
    maxRequestsPerMinute: 'RPM 限制',
    maxRequestsPerDayPerIp: '每日单 IP 限制',
    keyRotationStrategy: '密钥轮换策略',
  }
  for (const key of Object.keys(localConfig)) {
    if (localConfig[key] !== dashboardStore.config[key]) {
      try {
        await dashboardStore.updateConfig(key, localConfig[key], passwordFromParent)
        dashboardStore.config[key] = localConfig[key]
        messages.push(`${labelMap[key] || key}成功`)
      } catch (e) {
        allSucceeded = false
        messages.push(`${labelMap[key] || key}失败：${e.message}`)
      }
    }
  }
  if (allSucceeded && !messages.length) return { success: true, message: '基础配置：无变更' }
  return { success: allSucceeded, message: `基础配置：${messages.join('；')}` }
}

defineExpose({ saveComponentConfigs, localConfig })
</script>

<template>
  <div class="sub-section">
    <div class="sub-section__title">基础设置</div>
    <div class="grid grid--2">
      <div class="field">
        <label class="field__label">RPM 限制</label>
        <input
          v-model.number="localConfig.maxRequestsPerMinute"
          type="number"
          min="0"
          class="input"
        >
        <div class="field__hint">每个 IP 每分钟最大请求数。</div>
      </div>
      <div class="field">
        <label class="field__label">每日单 IP 限制</label>
        <input
          v-model.number="localConfig.maxRequestsPerDayPerIp"
          type="number"
          min="0"
          class="input"
        >
        <div class="field__hint">每个 IP 每日最大请求数。</div>
      </div>
    </div>

    <div class="field">
      <label class="field__label">密钥轮换策略</label>
      <div class="radio-row">
        <label class="radio-card" :class="{ 'radio-card--active': localConfig.keyRotationStrategy === 'fill' }">
          <input
            type="radio"
            value="fill"
            v-model="localConfig.keyRotationStrategy"
            name="keyRotationStrategy"
          >
          <span class="radio-card__title">填充模式（粘性，默认）</span>
          <span class="radio-card__desc">
            持续使用同一密钥，直至其触发配额或进入冷却后再切换。对外呈现单一用户的 RPM
            特征，不易被风控识别为密钥池。
          </span>
        </label>
        <label class="radio-card" :class="{ 'radio-card--active': localConfig.keyRotationStrategy === 'polling' }">
          <input
            type="radio"
            value="polling"
            v-model="localConfig.keyRotationStrategy"
            name="keyRotationStrategy"
          >
          <span class="radio-card__title">轮询模式（轮转）</span>
          <span class="radio-card__desc">
            原始行为——每次请求轮换到不同密钥。适合明确的单密钥负载均衡，
            但会暴露多密钥特征。
          </span>
        </label>
      </div>
      <div class="field__hint">
        切换立即生效并重置密钥栈，下一次请求将按新策略选取密钥。
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
