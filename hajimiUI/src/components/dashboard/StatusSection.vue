<script setup>
import { managementRequest } from '../../services/http'
import AppDialog from '../ui/AppDialog.vue'
import { useDashboardStore } from '../../stores/dashboard'
import { onBeforeUnmount, ref } from 'vue'
import StatusStats from './status/StatusStats.vue'
import ApiKeyStats from './status/ApiKeyStats.vue'

const dashboardStore = useDashboardStore()

const showResetDialog = ref(false)
const resetPassword = ref('')
const resetError = ref('')
const isResetting = ref(false)
let resetController
onBeforeUnmount(() => resetController?.abort())

function openResetDialog() {
  showResetDialog.value = true
  resetPassword.value = dashboardStore.sessionPassword || ''
  resetError.value = ''
}

function closeResetDialog() {
  showResetDialog.value = false
  resetError.value = ''
}

async function resetStats() {
  if (isResetting.value) return
  if (!resetPassword.value) {
    resetError.value = '请输入密码'
    return
  }
  const assertCurrent = dashboardStore.captureSession()
  dashboardStore.invalidateDashboardFetch()
  dashboardStore.stopLiveData()
  isResetting.value = true
  resetController = new AbortController()
  resetError.value = ''
  try {
    await managementRequest('/api/reset-stats', { password: resetPassword.value }, { signal: resetController.signal })
    assertCurrent()
    dashboardStore.invalidateDashboardFetch()
    dashboardStore.stopLiveData()
    await dashboardStore.fetchDashboardData()
    await dashboardStore.fetchLiveData()
    closeResetDialog()
  } catch (e) {
    resetError.value = e.message || '重置失败'
  } finally {
    isResetting.value = false
  }
}
</script>

<template>
  <section class="section">
    <div class="section__header">
      <div class="section__title">运行状态</div>
      <div class="row">
        <span class="pill" :class="dashboardStore.lastError ? 'pill--warning' : 'pill--success'">
          <span class="pill__dot pill__dot--pulse" />
          {{ dashboardStore.lastError ? '数据待更新' : '运行中' }}
        </span>
        <button
          v-if="!dashboardStore.status.enableVertex"
          class="btn btn--secondary btn--sm"
          @click="openResetDialog"
        >
          ↻ 重置计数
        </button>
      </div>
    </div>

    <StatusStats />
    <ApiKeyStats />

    <!-- Reset dialog -->
    <AppDialog v-model="showResetDialog" title="重置计数" :busy="isResetting" v-slot="{ close }">
        <div class="modal__header">
          <div class="modal__title">重置计数</div>
          <button class="btn btn--ghost btn--icon btn--sm" @click="close" :disabled="isResetting" aria-label="关闭重置计数窗口">✕</button>
        </div>
        <div class="modal__body">
          <p class="text-muted mb-4" style="font-size:var(--fs-sm);">
            此操作将清空所有 API 调用与令牌计数，且不可撤销。
          </p>
          <div class="field">
            <label class="field__label">管理密码</label>
            <input
              v-model="resetPassword"
              type="password"
              class="input"
              placeholder="输入密码确认"
              autocomplete="current-password"
              @keyup.enter="resetStats"
            >
            <div v-if="resetError" class="field__hint" style="color:var(--danger-strong);">
              {{ resetError }}
            </div>
          </div>
        </div>
        <div class="modal__footer">
          <button class="btn btn--secondary btn--sm" @click="close" :disabled="isResetting">取消</button>
          <button class="btn btn--primary btn--sm" @click="resetStats" :disabled="isResetting">
            {{ isResetting ? '重置中…' : '确认重置' }}
          </button>
        </div>
      </AppDialog>
  </section>
</template>
