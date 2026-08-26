<script setup>
import { useDashboardStore } from '../../stores/dashboard'
import { ref, computed } from 'vue'
import BasicConfig from './config/BasicConfig.vue'
import FeaturesConfig from './config/FeaturesConfig.vue'
import VersionInfo from './config/VersionInfo.vue'
import VertexConfig from './config/VertexConfig.vue'

const dashboardStore = useDashboardStore()
const isExpanded = ref(false)
const basicConfigRef = ref(null)
const featuresConfigRef = ref(null)
const sharedPassword = ref('')
const overallError = ref('')
const overallSuccess = ref('')
const isOverallSaving = ref(false)

const enableVertex = computed(() => dashboardStore.status.enableVertex)

function toggleExpand() {
  isExpanded.value = !isExpanded.value
  if (isExpanded.value && !sharedPassword.value) {
    // Pre-fill from session password if available
    sharedPassword.value = dashboardStore.sessionPassword || ''
  }
}

async function handleSaveAll() {
  if (!sharedPassword.value) {
    overallError.value = '请输入管理密码'
    overallSuccess.value = ''
    return
  }
  isOverallSaving.value = true
  overallError.value = ''
  overallSuccess.value = ''
  const errors = []
  const successes = []
  try {
    if (basicConfigRef.value?.saveComponentConfigs) {
      const r = await basicConfigRef.value.saveComponentConfigs(sharedPassword.value)
      if (r.success) successes.push(r.message)
      else errors.push(r.message)
    }
    if (featuresConfigRef.value?.saveComponentConfigs) {
      const r = await featuresConfigRef.value.saveComponentConfigs(sharedPassword.value)
      if (r.success) successes.push(r.message)
      else errors.push(r.message)
    }
    if (errors.length) overallError.value = errors.join('；')
    if (successes.length && !errors.length) overallSuccess.value = '已保存：' + successes.join('；')
    else if (successes.length && errors.length) overallSuccess.value = '部分保存成功：' + successes.join('；')
    await dashboardStore.fetchDashboardData()
  } catch (e) {
    overallError.value = e.message || '保存失败'
  } finally {
    isOverallSaving.value = false
  }
}
</script>

<template>
  <section class="section">
    <div class="section__header">
      <div class="section__title">服务配置</div>
      <button
        v-if="!enableVertex"
        class="btn btn--secondary btn--sm"
        @click="toggleExpand"
      >
        {{ isExpanded ? '收起' : '编辑' }}
      </button>
    </div>

    <!-- Vertex 模式：仅显示 Vertex 配置与版本 -->
    <template v-if="enableVertex">
      <VertexConfig />
      <VersionInfo />
    </template>

    <!-- 非 Vertex 模式 -->
    <template v-else>
      <!-- 折叠时的概览卡片 -->
      <div v-if="!isExpanded" class="card">
        <div class="card__body">
          <div class="grid grid--3">
            <div class="kpi-mini">
              <div class="kpi-mini__label">RPM 限制</div>
              <div class="kpi-mini__value mono">{{ dashboardStore.config.maxRequestsPerMinute }}</div>
            </div>
            <div class="kpi-mini">
              <div class="kpi-mini__label">并发数</div>
              <div class="kpi-mini__value mono">{{ dashboardStore.config.concurrentRequests }}</div>
            </div>
            <div class="kpi-mini">
              <div class="kpi-mini__label">服务器时间</div>
              <div class="kpi-mini__value mono" style="font-size:var(--fs-sm);">{{ dashboardStore.config.currentTime || '—' }}</div>
            </div>
          </div>
        </div>
      </div>

      <!-- 展开后的编辑器 -->
      <div v-else class="card">
        <div class="card__body" style="display:flex;flex-direction:column;gap:var(--sp-5);">
          <BasicConfig ref="basicConfigRef" />
          <FeaturesConfig ref="featuresConfigRef" />

          <div v-if="overallError" class="banner banner--danger">{{ overallError }}</div>
          <div v-if="overallSuccess" class="banner banner--success">{{ overallSuccess }}</div>

          <div class="save-row">
            <div class="field save-row__pw">
              <label class="field__label">管理密码</label>
              <input
                v-model="sharedPassword"
                type="password"
                class="input"
                autocomplete="current-password"
                placeholder="保存时必填"
              >
            </div>
            <div class="row save-row__btns">
              <button class="btn btn--secondary btn--sm" @click="isExpanded = false">取消</button>
              <button
                class="btn btn--primary btn--sm"
                @click="handleSaveAll"
                :disabled="isOverallSaving"
              >
                {{ isOverallSaving ? '保存中…' : '全部保存' }}
              </button>
            </div>
          </div>
        </div>
      </div>

      <VersionInfo />
    </template>
  </section>
</template>

<style scoped>
.kpi-mini {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.kpi-mini__label {
  font-size: var(--fs-xs);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-muted);
  font-weight: var(--fw-medium);
}
.kpi-mini__value {
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
  color: var(--text-strong);
}

/* 底部保存行：密码输入 + 操作按钮，窄屏自动堆叠 */
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
  .save-row__btns {
    justify-content: flex-end;
  }
}

.banner--success {
  background: var(--success-subtle);
  border-color: var(--success);
  color: var(--success-strong);
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--r-md);
  font-size: var(--fs-sm);
  border: 1px solid var(--success);
}
.banner--danger {
  background: var(--danger-subtle);
  border-color: var(--danger);
  color: var(--danger-strong);
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--r-md);
  font-size: var(--fs-sm);
  border: 1px solid var(--danger);
}
</style>
