<script setup>
import { useDashboardStore } from '../../../stores/dashboard'
import { reactive, ref, watch, computed } from 'vue'

const dashboardStore = useDashboardStore()

const localConfig = reactive({
  searchMode: false,
  searchPrompt: '',
  maxRetryNum: 0,
  fakeStreaming: false,
  fakeStreamingInterval: 0,
  randomString: false,
  randomStringLength: 0,
  concurrentRequests: 1,
  increaseConcurrentOnFailure: 0,
  maxConcurrentRequests: 1,
  maxEmptyResponses: 0,
  responsesDefaultModel: '',
  responsesModelAliases: [],
  claudeDefaultModel: '',
  claudeModelAliases: [],
})

const populatedFromStore = ref(false)

function aliasesToRows(aliases) {
  return Object.entries(aliases || {}).map(([alias, model]) => ({ alias, model }))
}

watch(
  () => ({
    ...dashboardStore.config,
    isLoaded: dashboardStore.isConfigLoaded,
  }),
  (v) => {
    if (!v.isLoaded || populatedFromStore.value) return
    Object.assign(localConfig, {
      searchMode: v.searchMode,
      searchPrompt: v.searchPrompt,
      maxRetryNum: v.maxRetryNum,
      fakeStreaming: v.fakeStreaming,
      fakeStreamingInterval: v.fakeStreamingInterval,
      randomString: v.randomString,
      randomStringLength: v.randomStringLength,
      concurrentRequests: v.concurrentRequests,
      increaseConcurrentOnFailure: v.increaseConcurrentOnFailure,
      maxConcurrentRequests: v.maxConcurrentRequests,
      maxEmptyResponses: v.maxEmptyResponses,
      responsesDefaultModel: v.responsesDefaultModel || '',
      responsesModelAliases: aliasesToRows(v.responsesModelAliases),
      claudeDefaultModel: v.claudeDefaultModel || '',
      claudeModelAliases: aliasesToRows(v.claudeModelAliases),
    })
    populatedFromStore.value = true
  },
  { deep: true, immediate: true }
)

function buildAliasMap(rows) {
  const result = {}
  for (const item of rows) {
    const alias = (item.alias || '').trim()
    const model = (item.model || '').trim()
    if (alias && model) result[alias] = model
  }
  return result
}

async function saveModelMapping(endpoint, defaultModel, aliases, password) {
  const response = await fetch(endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      default_model: defaultModel,
      aliases,
      password,
    }),
  })
  if (!response.ok) {
    const e = await response.json().catch(() => ({}))
    throw new Error(e.detail || e.error?.message || 'Save mapping failed')
  }
  return response.json()
}

function addResponseAlias() {
  localConfig.responsesModelAliases.push({
    alias: '',
    model: localConfig.responsesDefaultModel || '',
  })
}
function removeResponseAlias(i) {
  localConfig.responsesModelAliases.splice(i, 1)
}
function addClaudeAlias() {
  localConfig.claudeModelAliases.push({
    alias: '',
    model: localConfig.claudeDefaultModel || '',
  })
}
function removeClaudeAlias(i) {
  localConfig.claudeModelAliases.splice(i, 1)
}

async function saveComponentConfigs(passwordFromParent) {
  if (!passwordFromParent) {
    return { success: false, message: 'Features: password missing' }
  }
  let allSucceeded = true
  const messages = []
  const mappingKeys = [
    'responsesDefaultModel',
    'responsesModelAliases',
    'claudeDefaultModel',
    'claudeModelAliases',
  ]
  for (const key of Object.keys(localConfig).filter((k) => !mappingKeys.includes(k))) {
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
  try {
    await saveModelMapping(
      '/api/update-responses-model-mapping',
      localConfig.responsesDefaultModel,
      buildAliasMap(localConfig.responsesModelAliases),
      passwordFromParent
    )
    messages.push('responses mapping ok')
  } catch (e) {
    allSucceeded = false
    messages.push('responses mapping fail: ' + e.message)
  }
  try {
    await saveModelMapping(
      '/api/update-claude-model-mapping',
      localConfig.claudeDefaultModel,
      buildAliasMap(localConfig.claudeModelAliases),
      passwordFromParent
    )
    messages.push('claude mapping ok')
  } catch (e) {
    allSucceeded = false
    messages.push('claude mapping fail: ' + e.message)
  }
  if (allSucceeded && !messages.length) {
    return { success: true, message: 'Features: no changes' }
  }
  return { success: allSucceeded, message: `Features: ${messages.join('; ')}` }
}

defineExpose({ saveComponentConfigs, localConfig })
</script>

<template>
  <div class="sub-section">
    <div class="sub-section__title">Features</div>

    <!-- Toggles -->
    <div class="grid grid--2">
      <div class="field">
        <label class="field__label">Search mode</label>
        <div class="row row--between">
          <span class="text-muted" style="font-size:var(--fs-sm);">Append a web-search tool to requests.</span>
          <button class="toggle" :class="{ 'toggle--on': localConfig.searchMode }" @click="localConfig.searchMode = !localConfig.searchMode">
            <span class="toggle__thumb"></span>
          </button>
        </div>
      </div>
      <div class="field">
        <label class="field__label">Fake streaming</label>
        <div class="row row--between">
          <span class="text-muted" style="font-size:var(--fs-sm);">Chunked SSE replay of non-streaming calls.</span>
          <button class="toggle" :class="{ 'toggle--on': localConfig.fakeStreaming }" @click="localConfig.fakeStreaming = !localConfig.fakeStreaming">
            <span class="toggle__thumb"></span>
          </button>
        </div>
      </div>
    </div>

    <div v-if="localConfig.searchMode" class="field">
      <label class="field__label">Search prompt</label>
      <textarea v-model="localConfig.searchPrompt" class="textarea" rows="3"></textarea>
    </div>

    <div v-if="localConfig.fakeStreaming" class="grid grid--2">
      <div class="field">
        <label class="field__label">Fake-stream interval (s)</label>
        <input v-model.number="localConfig.fakeStreamingInterval" type="number" min="0" step="0.1" class="input">
      </div>
    </div>

    <div class="grid grid--3">
      <div class="field">
        <label class="field__label">Concurrent requests</label>
        <input v-model.number="localConfig.concurrentRequests" type="number" min="1" class="input">
      </div>
      <div class="field">
        <label class="field__label">Max concurrent</label>
        <input v-model.number="localConfig.maxConcurrentRequests" type="number" min="1" class="input">
      </div>
      <div class="field">
        <label class="field__label">On failure delta</label>
        <input v-model.number="localConfig.increaseConcurrentOnFailure" type="number" min="0" class="input">
        <div class="field__hint">0 = decrease on failure (recommended).</div>
      </div>
    </div>

    <div class="grid grid--3">
      <div class="field">
        <label class="field__label">Max retries</label>
        <input v-model.number="localConfig.maxRetryNum" type="number" min="0" class="input">
      </div>
      <div class="field">
        <label class="field__label">Max empty responses</label>
        <input v-model.number="localConfig.maxEmptyResponses" type="number" min="0" class="input">
      </div>
      <div class="field">
        <label class="field__label">Random string length</label>
        <input v-model.number="localConfig.randomStringLength" type="number" min="0" class="input" :disabled="!localConfig.randomString">
      </div>
    </div>

    <div class="grid grid--2">
      <div class="field">
        <label class="field__label">Responses default model</label>
        <input v-model="localConfig.responsesDefaultModel" type="text" class="input">
      </div>
      <div class="field">
        <label class="field__label">Claude default model</label>
        <input v-model="localConfig.claudeDefaultModel" type="text" class="input">
      </div>
    </div>

    <!-- Responses aliases -->
    <div class="sub-block">
      <div class="row row--between">
        <span class="text-muted" style="font-size:var(--fs-sm);font-weight:500;">Responses aliases</span>
        <button class="btn btn--secondary btn--sm" @click="addResponseAlias">+ Add</button>
      </div>
      <div v-for="(row, i) in localConfig.responsesModelAliases" :key="'r'+i" class="alias-row">
        <input v-model="row.alias" placeholder="alias" class="input">
        <span class="text-subtle">→</span>
        <input v-model="row.model" placeholder="model" class="input">
        <button class="btn btn--ghost btn--icon btn--sm" @click="removeResponseAlias(i)">✕</button>
      </div>
    </div>

    <!-- Claude aliases -->
    <div class="sub-block">
      <div class="row row--between">
        <span class="text-muted" style="font-size:var(--fs-sm);font-weight:500;">Claude aliases</span>
        <button class="btn btn--secondary btn--sm" @click="addClaudeAlias">+ Add</button>
      </div>
      <div v-for="(row, i) in localConfig.claudeModelAliases" :key="'c'+i" class="alias-row">
        <input v-model="row.alias" placeholder="alias" class="input">
        <span class="text-subtle">→</span>
        <input v-model="row.model" placeholder="model" class="input">
        <button class="btn btn--ghost btn--icon btn--sm" @click="removeClaudeAlias(i)">✕</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.sub-section {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
  padding-bottom: var(--sp-5);
  border-bottom: 1px solid var(--border);
}
.sub-section__title {
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-muted);
}
.sub-block {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.alias-row {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.alias-row .input { flex: 1; }
</style>
