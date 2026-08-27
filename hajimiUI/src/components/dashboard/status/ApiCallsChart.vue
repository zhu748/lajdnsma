<script setup>
import { ref, onMounted, onUnmounted, watch, nextTick, computed } from 'vue'
import { useDashboardStore } from '../../../stores/dashboard'
import * as echarts from 'echarts'

const dashboardStore = useDashboardStore()
const chartContainer = ref(null)
let chart = null

const chartData = ref({
  timestamps: [],
  apiCalls: [],
  tokens: [],
})

const isDark = computed(() => dashboardStore.isDarkMode)

function buildOption() {
  const dark = isDark.value
  const textColor = dark ? '#A1A1AA' : '#71717A'
  const gridColor = dark ? '#27272A' : '#E4E4E7'
  return {
    grid: { left: 8, right: 8, top: 8, bottom: 8, containLabel: true },
    tooltip: {
      trigger: 'axis',
      backgroundColor: dark ? '#18181B' : '#FFFFFF',
      borderColor: dark ? '#27272A' : '#E4E4E7',
      borderWidth: 1,
      textStyle: { color: dark ? '#FAFAFA' : '#18181B', fontSize: 12 },
      axisPointer: {
        type: 'line',
        lineStyle: { color: '#6366F1', type: 'dashed' },
      },
    },
    legend: {
      data: ['调用次数', '令牌数'],
      bottom: 0,
      icon: 'circle',
      itemWidth: 6,
      itemHeight: 6,
      textStyle: { color: textColor, fontSize: 11 },
    },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: chartData.value.timestamps,
      axisLabel: { color: textColor, fontSize: 10 },
      axisLine: { lineStyle: { color: gridColor } },
      axisTick: { show: false },
      splitLine: { show: false },
    },
    yAxis: [
      {
        type: 'value',
        axisLabel: { color: textColor, fontSize: 10 },
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { lineStyle: { color: gridColor, type: 'dashed' } },
      },
      {
        type: 'value',
        axisLabel: { color: textColor, fontSize: 10 },
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { show: false },
      },
    ],
    series: [
      {
        name: '调用次数',
        type: 'line',
        smooth: true,
        showSymbol: false,
        data: chartData.value.apiCalls,
        lineStyle: { width: 2, color: '#6366F1' },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(99,102,241,0.20)' },
            { offset: 1, color: 'rgba(99,102,241,0)' },
          ]),
        },
      },
      {
        name: '令牌数',
        type: 'line',
        yAxisIndex: 1,
        smooth: true,
        showSymbol: false,
        data: chartData.value.tokens,
        lineStyle: { width: 2, color: '#10B981' },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(16,185,129,0.18)' },
            { offset: 1, color: 'rgba(16,185,129,0)' },
          ]),
        },
      },
    ],
  }
}

function refreshChart() {
  if (!chart) return
  const series = dashboardStore.timeSeriesData
  // Fix(round4): 后端 get_time_series_data() 返回的点结构是 {time, value}，
  // 此前这里读的是 p.count —— 永远 undefined → || 0 兜底，导致图表
  // 无论真实调用量多少都恒画 0 的平线。现在优先读 value，同时兼容
  // 可能返回 count 的旧载荷。
  chartData.value = {
    timestamps: (series.calls || []).map((p) => p.time || ''),
    apiCalls: (series.calls || []).map((p) => p.value ?? p.count ?? 0),
    tokens: (series.tokens || []).map((p) => p.value ?? p.count ?? 0),
  }
  chart.setOption(buildOption(), { notMerge: true })
}

function initChart() {
  if (!chartContainer.value) return
  chart = echarts.init(chartContainer.value)
  refreshChart()
}

function handleResize() {
  chart && chart.resize()
}

watch(
  () => dashboardStore.timeSeriesData,
  () => {
    nextTick(refreshChart)
  }
  // Perf(round4): 移除 deep: true —— store 每次轮询都是整体替换
  // timeSeriesData.value.calls / .tokens 的数组引用，浅层 watch 即可
  // 触发；deep 模式会额外全树遍历 31 个数据点 × 2 条序列，转而变成
  // 每 5s 一次的纯浪费。
)

watch(isDark, () => {
  nextTick(refreshChart)
})

onMounted(() => {
  initChart()
  window.addEventListener('resize', handleResize)
})

onUnmounted(() => {
  window.removeEventListener('resize', handleResize)
  if (chart) {
    chart.dispose()
    chart = null
  }
})
</script>

<template>
  <div ref="chartContainer" class="chart"></div>
</template>

<style scoped>
.chart {
  height: 200px;
  width: 100%;
  padding: var(--sp-3) var(--sp-4);
  border-bottom: 1px solid var(--border);
}

/* 移动端：图表降高，减少纵向占用 */
@media (max-width: 768px) {
  .chart {
    height: 160px;
    padding: var(--sp-2) var(--sp-3);
  }
}
</style>
