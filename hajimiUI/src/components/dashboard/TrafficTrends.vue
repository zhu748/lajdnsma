<script setup>
import { computed, defineAsyncComponent, ref } from 'vue'
import { useDashboardStore } from '../../stores/dashboard'
const Chart = defineAsyncComponent(() => import('./status/ApiCallsChart.vue'))
const store = useDashboardStore()
const mode = ref('calls')
const points = computed(() => store.monitor.time_series || [])
const hasData = computed(() => points.value.some(p => p.calls || p.tokens || p.failures))
const series = computed(() => ({
  calls: points.value.map(p => ({time: new Date(p.timestamp).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false}), value: p[mode.value] || 0})),
  tokens: points.value.map(p => ({value:p.tokens || 0})),
}))
</script>
<template><section class="section"><div class="section__header"><div><h2 class="section__title">网关请求趋势</h2><p class="section-description">两种后端的客户端请求完成量与已知结果 Token；最近 30 分钟，按本地时间展示。</p></div><select v-model="mode" class="input" style="width:auto" aria-label="趋势请求指标"><option value="calls">全部结束请求</option><option value="failures">失败与中断</option></select></div><div class="card"><Chart v-if="hasData" :series="series" :caption="`最近 30 分钟 · ${mode === 'calls' ? '结束请求' : '失败与中断'}（左轴）· 已知结果 Token（右轴）`"/><div v-else class="empty-state"><div class="empty-state__title">暂无请求趋势</div><div class="empty-state__desc">请求结束后将显示数据；未知用量不当作实际零消耗。</div></div></div></section></template>
