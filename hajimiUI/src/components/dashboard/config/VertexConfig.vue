<script setup>
import { useDashboardStore } from '../../../stores/dashboard'
import { createConfigDraft } from '../../../composables/configDraft'
import { useConfigSaveGuard } from '../../../composables/configSaveGuard'
import { ref, reactive, watch } from 'vue'

const dashboardStore = useDashboardStore()
const beginSave = useConfigSaveGuard(dashboardStore)
const localConfig = reactive({
  fakeStreaming: false, enableVertexExpress: false,
  vertexExpressApiKey: '', googleCredentialsJson: '',
})
const secretKeys = ['vertexExpressApiKey', 'googleCredentialsJson']
const syncDraft = createConfigDraft(localConfig)
watch(() => [dashboardStore.isConfigLoaded, dashboardStore.config.fakeStreaming, dashboardStore.config.enableVertexExpress],
  ([loaded, fakeStreaming, enableVertexExpress]) => {
    if (loaded) syncDraft({ fakeStreaming, enableVertexExpress })
  }, { immediate: true })

const password = ref('')
const errorMsg = ref('')
const successMsg = ref('')
const isSaving = ref(false)

async function saveAll() {
  if (isSaving.value) return
  if (!dashboardStore.isConfigLoaded) { errorMsg.value = '配置尚未加载，请稍后重试'; return }
  if (!password.value) { errorMsg.value = '请输入管理密码'; return }
  const submitted = { ...localConfig }
  const submittedPassword = password.value
  const assertCurrent = beginSave()
  isSaving.value = true
  errorMsg.value = ''
  successMsg.value = ''
  try {
    // Save credentials before enabling the corresponding backend mode.
    for (const key of [...secretKeys, 'fakeStreaming', 'enableVertexExpress']) {
      const secret = secretKeys.includes(key)
      if (secret ? !submitted[key].trim() : submitted[key] === dashboardStore.config[key]) continue
      assertCurrent()
      await dashboardStore.updateConfig(key, submitted[key], submittedPassword)
      assertCurrent()
      if (!secret) syncDraft.acknowledge(key, submitted[key])
      dashboardStore.config[key] = secret ? true : submitted[key]
      // Preserve newer edits made while this exact snapshot was being saved.
      if (secret && localConfig[key] === submitted[key]) localConfig[key] = ''
    }
    await dashboardStore.fetchDashboardData()
    assertCurrent()
    successMsg.value = '本次提交已保存；保存期间的新修改仍保留在表单中'
    if (password.value === submittedPassword) password.value = ''
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
              role="switch" :aria-checked="localConfig.fakeStreaming" aria-label="伪流式"
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
              role="switch" :aria-checked="localConfig.enableVertexExpress" aria-label="Vertex Express"
              @click="localConfig.enableVertexExpress = !localConfig.enableVertexExpress"
            >
              <span class="toggle__thumb"></span>
            </button>
          </div>
        </div>
      </div>

      <div class="field">
        <label class="field__label" for="vertex-key">Vertex Express API 密钥</label>
        <input
          id="vertex-key"
          v-model="localConfig.vertexExpressApiKey"
          type="password"
          class="input"
          :placeholder="dashboardStore.config.vertexExpressApiKey ? '已配置，留空保留现有密钥' : '粘贴 Vertex Express API 密钥'"
          autocomplete="off"
        >
      </div>

      <div class="field">
        <label class="field__label" for="vertex-credentials">Google 凭证 JSON</label>
        <textarea
          id="vertex-credentials"
          v-model="localConfig.googleCredentialsJson"
          class="textarea"
          rows="6"
          :placeholder="dashboardStore.config.googleCredentialsJson ? '已配置，留空保留现有凭证' : '粘贴服务账号 JSON 凭证'"
        ></textarea>
        <div class="field__hint">支持一个或多个 JSON 凭证对象。留空保留已有凭证，保存成功后清空已提交的凭证输入。</div>
      </div>

      <div v-if="errorMsg" role="alert" class="banner banner--danger">{{ errorMsg }}</div>
      <div v-if="successMsg" role="status" class="banner banner--success">{{ successMsg }}</div>

      <div class="save-row">
        <div class="field save-row__pw">
          <label class="field__label" for="vertex-password">管理密码</label>
          <input
            id="vertex-password"
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
