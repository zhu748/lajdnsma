<script setup>
import { useDashboardStore } from '../../../stores/dashboard'
import { reactive, ref, watch } from 'vue'
import ModelMappingPanel from './ModelMappingPanel.vue'

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
  claudeModelAliases: []
})

const populatedFromStore = ref(false)

function aliasesToRows(aliases) {
  return Object.entries(aliases || {}).map(([alias, model]) => ({ alias, model }))
}

watch(
  () => ({
    searchMode: dashboardStore.config.searchMode,
    searchPrompt: dashboardStore.config.searchPrompt,
    maxRetryNum: dashboardStore.config.maxRetryNum,
    fakeStreaming: dashboardStore.config.fakeStreaming,
    fakeStreamingInterval: dashboardStore.config.fakeStreamingInterval,
    randomString: dashboardStore.config.randomString,
    randomStringLength: dashboardStore.config.randomStringLength,
    concurrentRequests: dashboardStore.config.concurrentRequests,
    increaseConcurrentOnFailure: dashboardStore.config.increaseConcurrentOnFailure,
    maxConcurrentRequests: dashboardStore.config.maxConcurrentRequests,
    maxEmptyResponses: dashboardStore.config.maxEmptyResponses,
    responsesDefaultModel: dashboardStore.config.responsesDefaultModel,
    responsesModelAliases: dashboardStore.config.responsesModelAliases,
    claudeDefaultModel: dashboardStore.config.claudeDefaultModel,
    claudeModelAliases: dashboardStore.config.claudeModelAliases,
    isLoaded: dashboardStore.isConfigLoaded
  }),
  (values) => {
    if (!values.isLoaded || populatedFromStore.value) return
    localConfig.searchMode = values.searchMode
    localConfig.searchPrompt = values.searchPrompt
    localConfig.maxRetryNum = values.maxRetryNum
    localConfig.fakeStreaming = values.fakeStreaming
    localConfig.fakeStreamingInterval = values.fakeStreamingInterval
    localConfig.randomString = values.randomString
    localConfig.randomStringLength = values.randomStringLength
    localConfig.concurrentRequests = values.concurrentRequests
    localConfig.increaseConcurrentOnFailure = values.increaseConcurrentOnFailure
    localConfig.maxConcurrentRequests = values.maxConcurrentRequests
    localConfig.maxEmptyResponses = values.maxEmptyResponses
    localConfig.responsesDefaultModel = values.responsesDefaultModel || ''
    localConfig.responsesModelAliases = aliasesToRows(values.responsesModelAliases)
    localConfig.claudeDefaultModel = values.claudeDefaultModel || ''
    localConfig.claudeModelAliases = aliasesToRows(values.claudeModelAliases)
    populatedFromStore.value = true
  },
  { deep: true, immediate: true }
)

function getBooleanText(value) {
  return value ? '??' : '??'
}

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
    body: JSON.stringify({ default_model: defaultModel, aliases, password })
  })
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}))
    throw new Error(errorData.detail || errorData.error?.message || '????????')
  }
  return response.json()
}

function addResponseAlias() {
  localConfig.responsesModelAliases.push({ alias: '', model: localConfig.responsesDefaultModel || '' })
}

function removeResponseAlias(index) {
  localConfig.responsesModelAliases.splice(index, 1)
}

function addClaudeAlias() {
  localConfig.claudeModelAliases.push({ alias: '', model: localConfig.claudeDefaultModel || '' })
}

function removeClaudeAlias(index) {
  localConfig.claudeModelAliases.splice(index, 1)
}

async function saveComponentConfigs(passwordFromParent) {
  if (!passwordFromParent) {
    return { success: false, message: '?????????????' }
  }

  let allSucceeded = true
  const messages = []
  const mappingKeys = [
    'responsesDefaultModel',
    'responsesModelAliases',
    'claudeDefaultModel',
    'claudeModelAliases'
  ]

  for (const key of Object.keys(localConfig).filter(key => !mappingKeys.includes(key))) {
    if (localConfig[key] !== dashboardStore.config[key]) {
      try {
        await dashboardStore.updateConfig(key, localConfig[key], passwordFromParent)
        dashboardStore.config[key] = localConfig[key]
        messages.push(`${key} ????`)
      } catch (error) {
        allSucceeded = false
        messages.push(`${key} ?????${error.message || '????'}`)
      }
    }
  }

  const responsesAliases = buildAliasMap(localConfig.responsesModelAliases)
  if (
    localConfig.responsesDefaultModel !== (dashboardStore.config.responsesDefaultModel || '') ||
    JSON.stringify(responsesAliases) !== JSON.stringify(dashboardStore.config.responsesModelAliases || {})
  ) {
    try {
      await saveModelMapping('/api/update-responses-model-mapping', localConfig.responsesDefaultModel, responsesAliases, passwordFromParent)
      dashboardStore.config.responsesDefaultModel = localConfig.responsesDefaultModel
      dashboardStore.config.responsesModelAliases = responsesAliases
      messages.push('Responses ????????')
    } catch (error) {
      allSucceeded = false
      messages.push(`Responses ?????????${error.message || '????'}`)
    }
  }

  const claudeAliases = buildAliasMap(localConfig.claudeModelAliases)
  if (
    localConfig.claudeDefaultModel !== (dashboardStore.config.claudeDefaultModel || '') ||
    JSON.stringify(claudeAliases) !== JSON.stringify(dashboardStore.config.claudeModelAliases || {})
  ) {
    try {
      await saveModelMapping('/api/update-claude-model-mapping', localConfig.claudeDefaultModel, claudeAliases, passwordFromParent)
      dashboardStore.config.claudeDefaultModel = localConfig.claudeDefaultModel
      dashboardStore.config.claudeModelAliases = claudeAliases
      messages.push('Claude ????????')
    } catch (error) {
      allSucceeded = false
      messages.push(`Claude ?????????${error.message || '????'}`)
    }
  }

  if (allSucceeded && messages.length === 0) {
    return { success: true, message: '??????????????' }
  }

  return { success: allSucceeded, message: `?????${messages.join('; ')}` }
}

defineExpose({ saveComponentConfigs, localConfig })
</script>

<template>
  <div class="features-config">
    <h3 class="section-title">????</h3>

    <div class="config-form">
      <div class="config-row">
        <div class="config-group">
          <label class="config-label">????</label>
          <div class="toggle-wrapper">
            <input id="searchMode" v-model="localConfig.searchMode" type="checkbox" class="toggle">
            <label for="searchMode" class="toggle-label"><span class="toggle-text">{{ getBooleanText(localConfig.searchMode) }}</span></label>
          </div>
        </div>

        <div class="config-group">
          <label class="config-label">?????</label>
          <div class="toggle-wrapper">
            <input id="fakeStreaming" v-model="localConfig.fakeStreaming" type="checkbox" class="toggle">
            <label for="fakeStreaming" class="toggle-label"><span class="toggle-text">{{ getBooleanText(localConfig.fakeStreaming) }}</span></label>
          </div>
        </div>

        <div class="config-group">
          <label class="config-label">????</label>
          <div class="toggle-wrapper">
            <input id="randomString" v-model="localConfig.randomString" type="checkbox" class="toggle">
            <label for="randomString" class="toggle-label"><span class="toggle-text">{{ getBooleanText(localConfig.randomString) }}</span></label>
          </div>
        </div>
      </div>

      <div class="config-row">
        <div class="config-group full-width">
          <label class="config-label">??????</label>
          <input v-model="localConfig.searchPrompt" type="text" class="config-input" placeholder="?????????">
        </div>
      </div>

      <div class="config-row">
        <div class="config-group">
          <label class="config-label">??????</label>
          <input v-model.number="localConfig.maxRetryNum" type="number" class="config-input" min="0">
        </div>
        <div class="config-group">
          <label class="config-label">????????</label>
          <input v-model.number="localConfig.fakeStreamingInterval" type="number" class="config-input" min="0" step="0.1">
        </div>
        <div class="config-group">
          <label class="config-label">???? length</label>
          <input v-model.number="localConfig.randomStringLength" type="number" class="config-input" min="0">
        </div>
      </div>

      <div class="config-row">
        <div class="config-group">
          <label class="config-label">???????</label>
          <input v-model.number="localConfig.concurrentRequests" type="number" class="config-input" min="1">
        </div>
        <div class="config-group">
          <label class="config-label">????????</label>
          <input v-model.number="localConfig.increaseConcurrentOnFailure" type="number" class="config-input" min="0">
        </div>
        <div class="config-group">
          <label class="config-label">???????</label>
          <input v-model.number="localConfig.maxConcurrentRequests" type="number" class="config-input" min="1">
        </div>
      </div>

      <div class="config-row">
        <div class="config-group">
          <label class="config-label">???????</label>
          <input v-model.number="localConfig.maxEmptyResponses" type="number" class="config-input" min="0">
        </div>
      </div>

      <ModelMappingPanel
        v-model:default-model="localConfig.responsesDefaultModel"
        title="Responses API ????"
        help="?? Codex CLI / OpenAI Responses ????????????? Gemini ???"
        :aliases="localConfig.responsesModelAliases"
        :available-models="dashboardStore.availableModels"
        alias-placeholder="????? codex-mini-latest ? gpt-*"
        empty-hint="???????????codex-mini-latest -> gemini-2.5-pro?gpt-* -> gemini-2.5-flash?"
        @add-alias="addResponseAlias"
        @remove-alias="removeResponseAlias"
      />

      <ModelMappingPanel
        v-model:default-model="localConfig.claudeDefaultModel"
        title="Claude API ????"
        help="?? Claude Code / Anthropic ????????????? Gemini ???"
        :aliases="localConfig.claudeModelAliases"
        :available-models="dashboardStore.availableModels"
        alias-placeholder="????? claude-sonnet-* ? claude-3-5-haiku-latest"
        empty-hint="???????????claude-sonnet-* -> gemini-2.5-pro?claude-* -> gemini-2.5-flash?"
        @add-alias="addClaudeAlias"
        @remove-alias="removeClaudeAlias"
      />
    </div>
  </div>
</template>

<style scoped>
.section-title {
  color: var(--color-heading);
  border-bottom: 1px solid var(--color-border);
  padding-bottom: 10px;
  margin-bottom: 20px;
  transition: all 0.3s ease;
  position: relative;
  font-weight: 600;
}

.section-title::after {
  content: '';
  position: absolute;
  bottom: -1px;
  left: 0;
  width: 50px;
  height: 2px;
  background: var(--gradient-primary);
}

.features-config {
  margin-bottom: 25px;
}

.config-form {
  background-color: var(--stats-item-bg);
  border-radius: var(--radius-lg);
  padding: 20px;
  box-shadow: var(--shadow-sm);
  border: 1px solid var(--card-border);
}

.config-row {
  display: flex;
  gap: 20px;
  margin-bottom: 20px;
}

.config-group {
  flex: 1;
  min-width: 0;
}

.config-group.full-width {
  flex-basis: 100%;
}

.config-label {
  display: block;
  margin-bottom: 8px;
  color: var(--color-heading);
  font-weight: 500;
}

.config-input {
  width: 100%;
  padding: 10px 12px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background-color: var(--color-background);
  color: var(--color-text);
  font-size: 14px;
}

.config-input:focus {
  outline: none;
  border-color: var(--button-primary);
  box-shadow: 0 0 0 2px rgba(79, 70, 229, 0.2);
}

.toggle-wrapper {
  display: flex;
  align-items: center;
  gap: 10px;
}

.toggle {
  width: 18px;
  height: 18px;
}

.toggle-label {
  cursor: pointer;
  color: var(--color-text);
}

.toggle-text {
  font-size: 14px;
}

@media (max-width: 768px) {
  .config-form {
    padding: 15px;
  }

  .config-row {
    flex-direction: column;
    gap: 15px;
    margin-bottom: 15px;
  }
}
</style>
