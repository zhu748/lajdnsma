<script setup>
import { useDashboardStore } from '../../../stores/dashboard'
import { reactive, ref, watch, computed } from 'vue'

const dashboardStore = useDashboardStore()

const localConfig = reactive({
  searchMode: false,
  searchPrompt: '',
  enableThinking: true,
  maxThinking: true,
  maxThinkingLevels: [],
  maxRetryNum: 0,
  fakeStreaming: false,
  fakeStreamingInterval: 0,
  randomString: false,
  randomStringLength: 0,
  concurrentRequests: 1,
  increaseConcurrentOnFailure: 0,
  maxConcurrentRequests: 1,
  maxEmptyResponses: 0,
  pvpMode: false,
  pvpKey: '',
  pvpMaxRetries: 50,
  responsesDefaultModel: '',
  responsesModelAliases: [],
  claudeDefaultModel: '',
  claudeModelAliases: [],
})

const populatedFromStore = ref(false)

function aliasesToRows(aliases) {
  return Object.entries(aliases || {}).map(([alias, model]) => ({ alias, model }))
}

function thinkingLevelsToRows(levels) {
  return Object.entries(levels || {}).map(([model, level]) => ({
    model,
    level: String(level),
  }))
}

// 思考等级合法值：等级 / off / dynamic / 数字 budget
const THINKING_LEVEL_RE = /^(minimal|low|medium|high|off|none|disable|disabled|dynamic|auto|-?\d+)$/i

function buildThinkingLevelMap() {
  const result = {}
  for (const row of localConfig.maxThinkingLevels) {
    const model = (row.model || '').trim()
    const level = String(row.level == null ? '' : row.level).trim()
    if (!model && !level) continue
    if (!model || !level) {
      throw new Error(`思考等级表第 ${localConfig.maxThinkingLevels.indexOf(row) + 1} 行未填写完整`)
    }
    if (!THINKING_LEVEL_RE.test(level)) {
      throw new Error(`模型 ${model} 的思考等级值无效：${level}（支持 minimal/low/medium/high、off、dynamic 或数字）`)
    }
    result[model] = level
  }
  return result
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
      enableThinking: v.enableThinking,
      maxThinking: v.maxThinking,
      maxThinkingLevels: thinkingLevelsToRows(v.maxThinkingModelLevels),
      maxRetryNum: v.maxRetryNum,
      fakeStreaming: v.fakeStreaming,
      fakeStreamingInterval: v.fakeStreamingInterval,
      randomString: v.randomString,
      randomStringLength: v.randomStringLength,
      concurrentRequests: v.concurrentRequests,
      increaseConcurrentOnFailure: v.increaseConcurrentOnFailure,
      maxConcurrentRequests: v.maxConcurrentRequests,
      maxEmptyResponses: v.maxEmptyResponses,
      pvpMode: v.pvpMode || false,
      pvpKey: v.pvpKey || '',
      pvpMaxRetries: v.pvpMaxRetries || 50,
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
    throw new Error(e.detail || e.error?.message || '保存模型映射失败')
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
function addThinkingLevel() {
  localConfig.maxThinkingLevels.push({ model: '', level: '' })
}
function removeThinkingLevel(i) {
  localConfig.maxThinkingLevels.splice(i, 1)
}

async function saveComponentConfigs(passwordFromParent) {
  if (!passwordFromParent) {
    return { success: false, message: '功能配置：缺少密码' }
  }
  let allSucceeded = true
  const messages = []
  const labelMap = {
    searchMode: '搜索模式',
    searchPrompt: '搜索提示词',
    enableThinking: '思考功能',
    maxThinking: '最高思考强度',
    maxRetryNum: '最大重试次数',
    fakeStreaming: '伪流式',
    fakeStreamingInterval: '伪流式间隔',
    randomString: '随机字符串',
    randomStringLength: '随机字符串长度',
    concurrentRequests: '并发请求数',
    increaseConcurrentOnFailure: '失败调整值',
    maxConcurrentRequests: '最大并发',
    maxEmptyResponses: '最大空响应次数',
    pvpMode: 'PVP模式',
    pvpKey: 'PVP指定Key',
    pvpMaxRetries: 'PVP最大重试次数',
  }
  const mappingKeys = [
    'responsesDefaultModel',
    'responsesModelAliases',
    'claudeDefaultModel',
    'claudeModelAliases',
    'maxThinkingLevels',
  ]
  for (const key of Object.keys(localConfig).filter((k) => !mappingKeys.includes(k))) {
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
  // 按模型思考等级（对象类型，需 JSON 对比 + 服务端校验）
  let levelMap = {}
  try {
    levelMap = buildThinkingLevelMap()
  } catch (e) {
    allSucceeded = false
    messages.push('思考等级表：' + e.message)
  }
  if (
    allSucceeded &&
    JSON.stringify(levelMap) !== JSON.stringify(dashboardStore.config.maxThinkingModelLevels || {})
  ) {
    try {
      await dashboardStore.updateConfig('maxThinkingModelLevels', levelMap, passwordFromParent)
      dashboardStore.config.maxThinkingModelLevels = levelMap
      messages.push(`按模型思考等级成功（共 ${Object.keys(levelMap).length} 项）`)
    } catch (e) {
      allSucceeded = false
      messages.push('按模型思考等级失败：' + (e.message || '未知错误'))
    }
  }

  try {
    await saveModelMapping(
      '/api/update-responses-model-mapping',
      localConfig.responsesDefaultModel,
      buildAliasMap(localConfig.responsesModelAliases),
      passwordFromParent
    )
    messages.push('Responses 模型映射成功')
  } catch (e) {
    allSucceeded = false
    messages.push('Responses 模型映射失败：' + e.message)
  }
  try {
    await saveModelMapping(
      '/api/update-claude-model-mapping',
      localConfig.claudeDefaultModel,
      buildAliasMap(localConfig.claudeModelAliases),
      passwordFromParent
    )
    messages.push('Claude 模型映射成功')
  } catch (e) {
    allSucceeded = false
    messages.push('Claude 模型映射失败：' + e.message)
  }
  if (allSucceeded && !messages.length) {
    return { success: true, message: '功能配置：无变更' }
  }
  return { success: allSucceeded, message: `功能配置：${messages.join('；')}` }
}

defineExpose({ saveComponentConfigs, localConfig })
</script>

<template>
  <div class="sub-section">
    <div class="sub-section__title">功能设置</div>

    <!-- 开关项 -->
    <div class="grid grid--2">
      <div class="field">
        <label class="field__label">搜索模式</label>
        <div class="row row--between">
          <span class="text-muted" style="font-size:var(--fs-sm);">为请求附加网页搜索工具。</span>
          <button class="toggle" :class="{ 'toggle--on': localConfig.searchMode }" @click="localConfig.searchMode = !localConfig.searchMode">
            <span class="toggle__thumb"></span>
          </button>
        </div>
      </div>
      <div class="field">
        <label class="field__label">伪流式</label>
        <div class="row row--between">
          <span class="text-muted" style="font-size:var(--fs-sm);">将非流式响应分块以 SSE 回放。</span>
          <button class="toggle" :class="{ 'toggle--on': localConfig.fakeStreaming }" @click="localConfig.fakeStreaming = !localConfig.fakeStreaming">
            <span class="toggle__thumb"></span>
          </button>
        </div>
      </div>
    </div>

    <div v-if="localConfig.searchMode" class="field">
      <label class="field__label">搜索提示词</label>
      <textarea v-model="localConfig.searchPrompt" class="textarea" rows="3"></textarea>
    </div>

    <!-- 思考强度设置 -->
    <div class="grid grid--2">
      <div class="field">
        <label class="field__label">思考功能</label>
        <div class="row row--between">
          <span class="text-muted" style="font-size:var(--fs-sm);">总开关：关闭后不注入任何思考参数。</span>
          <button class="toggle" :class="{ 'toggle--on': localConfig.enableThinking }" @click="localConfig.enableThinking = !localConfig.enableThinking">
            <span class="toggle__thumb"></span>
          </button>
        </div>
      </div>
      <div class="field">
        <label class="field__label">最高思考强度</label>
        <div class="row row--between">
          <span class="text-muted" style="font-size:var(--fs-sm);">忽略请求思考参数，强制各模型最高档。</span>
          <button class="toggle" :class="{ 'toggle--on': localConfig.maxThinking }" @click="localConfig.maxThinking = !localConfig.maxThinking">
            <span class="toggle__thumb"></span>
          </button>
        </div>
      </div>
    </div>

    <div v-if="localConfig.enableThinking" class="field__hint">
      最高档取值：2.5 Pro → budget 32768，2.5 Flash 系 → budget 24576，3.x / 4.x → thinkingLevel=high。
      优先级：按模型指定等级 &gt; 最高思考强度开关 &gt; 请求自带参数。不支持思考的模型（TTS / Embedding / Lyria 等）自动跳过。
    </div>

    <!-- 按模型指定思考等级 -->
    <div v-if="localConfig.enableThinking" class="sub-block">
      <div class="row row--between">
        <span class="text-muted" style="font-size:var(--fs-sm);font-weight:500;">按模型指定思考等级（优先于最高档开关）</span>
        <button class="btn btn--secondary btn--sm" @click="addThinkingLevel">+ 添加</button>
      </div>
      <div v-for="(row, i) in localConfig.maxThinkingLevels" :key="'t'+i" class="alias-row">
        <input v-model="row.model" list="thinking-model-options" placeholder="模型名 / 前缀（如 gemini-3.8-flash）" class="input">
        <span class="text-subtle">→</span>
        <input v-model="row.level" placeholder="high / medium / low / minimal / off / 数字" class="input">
        <button class="btn btn--ghost btn--icon btn--sm" @click="removeThinkingLevel(i)">✕</button>
      </div>
      <datalist id="thinking-model-options">
        <option v-for="m in dashboardStore.availableModels" :key="m" :value="m"></option>
      </datalist>
      <div class="field__hint">
        模型名支持精确匹配或最长前缀匹配（填 gemini-3.8 可覆盖全部 3.8 变体）。
        等级值支持 minimal / low / medium / high、off（关闭）、dynamic（2.5 动态）或数字 budget：
        2.5 系列直接使用数字（0=关闭，-1=动态，Pro 上限 32768，Flash 系上限 24576）；
        3.x / 4.x 模型的数字会就近换算成等级（≤2048→low、≤10240→medium、更高→high），避免 4.x 报 budget 400 错误。
      </div>
    </div>

    <div v-if="localConfig.fakeStreaming" class="grid grid--2">
      <div class="field">
        <label class="field__label">伪流式间隔（秒）</label>
        <input v-model.number="localConfig.fakeStreamingInterval" type="number" min="0" step="0.1" class="input">
      </div>
    </div>

    <!-- PVP 模式：指定 key 持续重试 -->
    <div class="grid grid--2">
      <div class="field">
        <label class="field__label">PVP 模式</label>
        <div class="row row--between">
          <span class="text-muted" style="font-size:var(--fs-sm);">钉住指定 Key 持续重试，直到出结果。</span>
          <button class="toggle" :class="{ 'toggle--on': localConfig.pvpMode }" @click="localConfig.pvpMode = !localConfig.pvpMode">
            <span class="toggle__thumb"></span>
          </button>
        </div>
      </div>
    </div>

    <div v-if="localConfig.pvpMode && !(localConfig.pvpKey || '').trim()" class="pvp-warning">
      未指定 Key，PVP 模式不会生效；请在下方填写指定 Key。
    </div>

    <div v-if="localConfig.pvpMode" class="grid grid--2">
      <div class="field">
        <label class="field__label">指定 Key</label>
        <input v-model="localConfig.pvpKey" list="pvp-key-options" type="text" class="input" placeholder="key#编号 / #序号 / 密钥尾片段">
        <datalist id="pvp-key-options">
          <option v-for="stat in dashboardStore.apiKeyStats" :key="stat.api_key" :value="stat.api_key"></option>
        </datalist>
        <div class="field__hint">可填密钥状态页的 key# 编号、池内序号（如 #0）或密钥尾片段（≥4 位）；留空则 PVP 不生效。</div>
      </div>
      <div class="field">
        <label class="field__label">PVP 最大重试次数</label>
        <input v-model.number="localConfig.pvpMaxRetries" type="number" min="1" class="input">
        <div class="field__hint">钉住的 Key 最多重试这么多次，防止无限重试。</div>
      </div>
    </div>

    <div class="grid grid--3">
      <div class="field">
        <label class="field__label">并发请求数</label>
        <input v-model.number="localConfig.concurrentRequests" type="number" min="1" class="input">
      </div>
      <div class="field">
        <label class="field__label">最大并发</label>
        <input v-model.number="localConfig.maxConcurrentRequests" type="number" min="1" class="input">
      </div>
      <div class="field">
        <label class="field__label">失败调整值</label>
        <input v-model.number="localConfig.increaseConcurrentOnFailure" type="number" min="0" class="input">
        <div class="field__hint">0 = 失败时降低并发（推荐）。</div>
      </div>
    </div>

    <div class="grid grid--3">
      <div class="field">
        <label class="field__label">最大重试次数</label>
        <input v-model.number="localConfig.maxRetryNum" type="number" min="0" class="input">
      </div>
      <div class="field">
        <label class="field__label">最大空响应次数</label>
        <input v-model.number="localConfig.maxEmptyResponses" type="number" min="0" class="input">
      </div>
      <div class="field">
        <label class="field__label">随机字符串长度</label>
        <input v-model.number="localConfig.randomStringLength" type="number" min="0" class="input" :disabled="!localConfig.randomString">
      </div>
    </div>

    <div class="grid grid--2">
      <div class="field">
        <label class="field__label">Responses 默认模型</label>
        <input v-model="localConfig.responsesDefaultModel" type="text" class="input">
      </div>
      <div class="field">
        <label class="field__label">Claude 默认模型</label>
        <input v-model="localConfig.claudeDefaultModel" type="text" class="input">
      </div>
    </div>

    <!-- Responses 别名 -->
    <div class="sub-block">
      <div class="row row--between">
        <span class="text-muted" style="font-size:var(--fs-sm);font-weight:500;">Responses 模型别名</span>
        <button class="btn btn--secondary btn--sm" @click="addResponseAlias">+ 添加</button>
      </div>
      <div v-for="(row, i) in localConfig.responsesModelAliases" :key="'r'+i" class="alias-row">
        <input v-model="row.alias" placeholder="别名" class="input">
        <span class="text-subtle">→</span>
        <input v-model="row.model" placeholder="模型名" class="input">
        <button class="btn btn--ghost btn--icon btn--sm" @click="removeResponseAlias(i)">✕</button>
      </div>
    </div>

    <!-- Claude 别名 -->
    <div class="sub-block">
      <div class="row row--between">
        <span class="text-muted" style="font-size:var(--fs-sm);font-weight:500;">Claude 模型别名</span>
        <button class="btn btn--secondary btn--sm" @click="addClaudeAlias">+ 添加</button>
      </div>
      <div v-for="(row, i) in localConfig.claudeModelAliases" :key="'c'+i" class="alias-row">
        <input v-model="row.alias" placeholder="别名" class="input">
        <span class="text-subtle">→</span>
        <input v-model="row.model" placeholder="模型名" class="input">
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

/* 窄屏：别名行去掉箭头符号，两个输入框平分宽度 */
@media (max-width: 640px) {
  .alias-row {
    flex-wrap: wrap;
  }
  .alias-row .input {
    min-width: 0;
    flex: 1 1 calc(50% - var(--sp-2));
  }
  .alias-row .text-subtle {
    display: none;
  }
}

.pvp-warning {
  font-size: var(--fs-sm);
  color: var(--warning-strong);
}
</style>
