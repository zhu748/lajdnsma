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
// 快照式脏检查基线：Vertex 凭证字段（vertexExpressApiKey /
// googleCredentialsJson）后端只回传「是否已配置」的布尔值，回填到
// 输入框会把字面量 "true" 写进 v-model——一旦用户碰过输入框就会把
// 字符串 "true" POST 成新凭证。现在凭证字段恒初始化为 ''（占位符
// 风格），脏检查改为对比「填充时快照」而非 store 值，避免空输入框
// 被误判为已修改而在每次 Save 时把已存凭证清空。
let initialConfig = null
watch(
  () => dashboardStore.isConfigLoaded,
  (loaded) => {
    if (loaded && !populated.value) {
      localConfig.fakeStreaming = dashboardStore.config.fakeStreaming
      localConfig.enableVertexExpress = dashboardStore.config.enableVertexExpress
      localConfig.vertexExpressApiKey = ''
      localConfig.googleCredentialsJson = ''
      initialConfig = { ...localConfig }
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
    errorMsg.value = '请输入管理密码'
    return
  }
  isSaving.value = true
  errorMsg.value = ''
  successMsg.value = ''
  try {
    for (const key of Object.keys(localConfig)) {
      if (localConfig[key] !== initialConfig[key]) {
        await dashboardStore.updateConfig(key, localConfig[key], password.value)
        initialConfig[key] = localConfig[key]
      }
    }
    successMsg.value = '已保存'
    await dashboardStore.fetchDashboardData()
    setTimeout(() => (successMsg.value = ''), 2000)
  } catch (e) {
    errorMsg.value = e.message || '保存失败'
  } finally {
    isSaving.value = false
  }
}
</script>

<template>
  <div class="card">
    <div class="card__header">
      <div class="card__title">Vertex 配置</div>
    </div>
    <div class="card__body" style="display:flex;flex-direction:column;gap:var(--sp-4);">
      <div class="grid grid--2">
        <div class="field">
          <label class="field__label">伪流式</label>
          <div class="row row--between">
            <span class="text-muted" style="font-size:var(--fs-sm);">将非流式响应分块以 SSE 回放。</span>
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
            <span class="text-muted" style="font-size:var(--fs-sm);">启用 Express 模式。</span>
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
        <label class="field__label">Vertex Express API 密钥</label>
        <input
          v-model="localConfig.vertexExpressApiKey"
          type="password"
          class="input"
          placeholder="粘贴 Vertex Express API 密钥"
          autocomplete="off"
        >
      </div>

      <div class="field">
        <label class="field__label">Google 凭证 JSON</label>
        <textarea
          v-model="localConfig.googleCredentialsJson"
          class="textarea"
          rows="6"
          placeholder='{ "type": "service_account", ... }'
        ></textarea>
        <div class="field__hint">粘贴一个或多个连续拼接的 JSON 凭证对象。</div>
      </div>

      <div v-if="errorMsg" class="banner banner--danger">{{ errorMsg }}</div>
      <div v-if="successMsg" class="banner banner--success">{{ successMsg }}</div>

      <div class="save-row">
        <div class="field save-row__pw">
          <label class="field__label">管理密码</label>
          <input
            v-model="password"
            type="password"
            class="input"
            autocomplete="current-password"
            placeholder="保存时必填"
          >
        </div>
        <button class="btn btn--primary btn--sm" @click="saveAll" :disabled="isSaving">
          {{ isSaving ? '保存中…' : '保存' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 底部保存行：窄屏自动堆叠 */
.save-row {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: var(--sp-4);
  border-top: 1px solid var(--border);
  padding-top: var(--sp-4);
}
.save-row__pw {
  flex: 1;
  max-width: 280px;
}
@media (max-width: 640px) {
  .save-row {
    flex-direction: column;
    align-items: stretch;
  }
  .save-row__pw {
    max-width: none;
  }
}

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
