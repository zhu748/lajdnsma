<script setup>
import { useDashboardStore } from '../../stores/dashboard'
import { ref } from 'vue'
import StatusStats from './status/StatusStats.vue'
import ApiKeyStats from './status/ApiKeyStats.vue'

const dashboardStore = useDashboardStore()

const showResetDialog = ref(false)
const resetPassword = ref('')
const resetError = ref('')
const isResetting = ref(false)

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
  if (!resetPassword.value) {
    resetError.value = 'Password required'
    return
  }
  isResetting.value = true
  resetError.value = ''
  try {
    const response = await fetch('/api/reset-stats', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: resetPassword.value }),
    })
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail || 'Reset failed')
    await dashboardStore.fetchDashboardData()
    setTimeout(async () => {
      await dashboardStore.fetchDashboardData()
      closeResetDialog()
    }, 500)
  } catch (e) {
    resetError.value = e.message || 'Reset failed'
  } finally {
    isResetting.value = false
  }
}
</script>

<template>
  <section class="section">
    <div class="section__header">
      <div class="section__title">Status</div>
      <div class="row">
        <span class="pill pill--success">
          <span class="pill__dot pill__dot--pulse" />
          Running
        </span>
        <button
          v-if="!dashboardStore.status.enableVertex"
          class="btn btn--secondary btn--sm"
          @click="openResetDialog"
        >
          ↻ Reset counters
        </button>
      </div>
    </div>

    <StatusStats />
    <ApiKeyStats />

    <!-- Reset dialog -->
    <div v-if="showResetDialog" class="modal-overlay" @click.self="closeResetDialog">
      <div class="modal">
        <div class="modal__header">
          <div class="modal__title">Reset counters</div>
          <button class="btn btn--ghost btn--icon btn--sm" @click="closeResetDialog">✕</button>
        </div>
        <div class="modal__body">
          <p class="text-muted mb-4" style="font-size:var(--fs-sm);">
            This will clear all API call and token counters. This action cannot be undone.
          </p>
          <div class="field">
            <label class="field__label">Operator password</label>
            <input
              v-model="resetPassword"
              type="password"
              class="input"
              placeholder="Confirm password"
              autocomplete="current-password"
              @keyup.enter="resetStats"
            >
            <div v-if="resetError" class="field__hint" style="color:var(--danger-strong);">
              {{ resetError }}
            </div>
          </div>
        </div>
        <div class="modal__footer">
          <button class="btn btn--secondary btn--sm" @click="closeResetDialog">Cancel</button>
          <button class="btn btn--primary btn--sm" @click="resetStats" :disabled="isResetting">
            {{ isResetting ? 'Resetting…' : 'Reset' }}
          </button>
        </div>
      </div>
    </div>
  </section>
</template>
