<script setup>
import { useDashboardStore } from '../../../stores/dashboard'
import { ref, reactive, watch } from 'vue'

const dashboardStore = useDashboardStore()

const localConfig = reactive({
  fakeStreaming: false,
  enableVertexExpress: false,
  vertexExpressApiKey: '',
  googleCredentialsJson: '',
})

const populated = ref(false)
watch(
  () => dashboardStore.isConfigLoaded,
  (loaded) => {
    if (loaded && !populated.value) {
      localConfig.fakeStreaming = dashboardStore.config.fakeStreaming
      localConfig.enableVertexExpress = dashboardStore.config.enableVertexExpress
      localConfig.vertexExpressApiKey = dashboardStore.config.vertexExpressApiKey || ''
      localConfig.googleCredentialsJson = dashboardStore.config.googleCredentialsJson || ''
      populated.value = true
    }
  },
  { immediate: true }
)

const password = ref('')
const errorMsg = ref('')
const successMsg = ref('')
const isSaving = ref(false)

async function saveAll() {
  if (!password.value) {
    errorMsg.value = 'Operator password required'
    return
  }
  isSaving.value = true
  errorMsg.value = ''
  successMsg.value = ''
  try {
    for (const key of Object.keys(localConfig)) {
      if (localConfig[key] !== dashboardStore.config[key]) {
        await dashboardStore.updateConfig(key, localConfig[key], password.value)
        dashboardStore.config[key] = localConfig[key]
      }
    }
    successMsg.value = 'Saved'
    await dashboardStore.fetchDashboardData()
    setTimeout(() => (successMsg.value = ''), 2000)
  } catch (e) {
    errorMsg.value = e.message || 'Save failed'
  } finally {
    isSaving.value = false
  }
}
</script>

<template>
  <div class="card">
    <div class="card__header">
      <div class="card__title">Vertex configuration</div>
    </div>
    <div class="card__body" style="display:flex;flex-direction:column;gap:var(--sp-4);">
      <div class="grid grid--2">
        <div class="field">
          <label class="field__label">Fake streaming</label>
          <div class="row row--between">
            <span class="text-muted" style="font-size:var(--fs-sm);">Chunked SSE replay of non-streaming calls.</span>
            <button
              class="toggle"
              :class="{ 'toggle--on': localConfig.fakeStreaming }"
              @click="localConfig.fakeStreaming = !localConfig.fakeStreaming"
            >
              <span class="toggle__thumb"></span>
            </button>
          </div>
        </div>
        <div class="field">
          <label class="field__label">Vertex Express</label>
          <div class="row row--between">
            <span class="text-muted" style="font-size:var(--fs-sm);">Enable Express mode.</span>
            <button
              class="toggle"
              :class="{ 'toggle--on': localConfig.enableVertexExpress }"
              @click="localConfig.enableVertexExpress = !localConfig.enableVertexExpress"
            >
              <span class="toggle__thumb"></span>
            </button>
          </div>
        </div>
      </div>

      <div class="field">
        <label class="field__label">Vertex Express API key</label>
        <input
          v-model="localConfig.vertexExpressApiKey"
          type="password"
          class="input"
          placeholder="Paste Vertex Express API key"
          autocomplete="off"
        >
      </div>

      <div class="field">
        <label class="field__label">Google credentials JSON</label>
        <textarea
          v-model="localConfig.googleCredentialsJson"
          class="textarea"
          rows="6"
          placeholder='{ "type": "service_account", ... }'
        ></textarea>
        <div class="field__hint">Paste one or more concatenated JSON credential objects.</div>
      </div>

      <div v-if="errorMsg" class="banner banner--danger">{{ errorMsg }}</div>
      <div v-if="successMsg" class="banner banner--success">{{ successMsg }}</div>

      <div class="row row--between" style="border-top:1px solid var(--border);padding-top:var(--sp-4);">
        <div class="field" style="flex:1;max-width:280px;">
          <label class="field__label">Operator password</label>
          <input
            v-model="password"
            type="password"
            class="input"
            autocomplete="current-password"
            placeholder="Required to save"
          >
        </div>
        <button class="btn btn--primary btn--sm" @click="saveAll" :disabled="isSaving">
          {{ isSaving ? 'Saving…' : 'Save' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.banner--success {
  background: var(--success-subtle);
  border: 1px solid var(--success);
  color: var(--success-strong);
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--r-md);
  font-size: var(--fs-sm);
}
.banner--danger {
  background: var(--danger-subtle);
  border: 1px solid var(--danger);
  color: var(--danger-strong);
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--r-md);
  font-size: var(--fs-sm);
}
</style>
