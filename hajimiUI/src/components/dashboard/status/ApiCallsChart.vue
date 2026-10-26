<script setup>
import { ref, onMounted, onUnmounted, watch, nextTick, computed } from 'vue'
import { useDashboardStore } from '../../../stores/dashboard'
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([LineChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer])

const props = defineProps({ series: { type: Object, default: null }, caption: { type: String, default: '最近 30 分钟 · 上游完成次数与已知用量 · 分钟桶' } })
const dashboardStore = useDashboardStore()
const chartContainer = ref(null)
let chart = null
let resizeObserver = null

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
    // 平滑动画：首次渲染 600ms，后续每 5s 数据刷新时 400ms 过渡，
    // 折线以插值方式滑动而不是瞬间跳变，观感更「实时」。
    animation: !window.matchMedia?.('(prefers-reduced-motion: reduce)').matches,
    color: [dark ? '#5EEAD4' : '#0F766E', '#609AC5'],
    textStyle: { fontFamily: 'Noto Sans SC, sans-serif' },
    animationDuration: 600,
    animationDurationUpdate: 400,
    animationEasing: 'cubicOut',
    animationEasingUpdate: 'cubicInOut',
    grid: { left: 32, right: 42, top: 16, bottom: 34, containLabel: true },
    tooltip: {
      trigger: 'axis',
      backgroundColor: dark ? '#18181B' : '#FFFFFF',
      borderColor: dark ? '#27272A' : '#E4E4E7',
      borderWidth: 1,
      textStyle: { color: dark ? '#FAFAFA' : '#18181B', fontSize: 12 },
      axisPointer: {
        type: 'line',
        lineStyle: { color: dark ? '#5EEAD4' : '#0F766E', type: 'dashed' },
      },
    },
    legend: {
      data: ['调用次数', '令牌数'], itemGap: 22,
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
        type: 'value', name: '次 / 分钟', nameLocation: 'middle', nameGap: 34,
        axisLabel: { color: textColor, fontSize: 10 },
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { lineStyle: { color: gridColor, type: 'dashed' } },
      },
      {
        type: 'value', name: 'Token / 分钟', nameLocation: 'middle', nameGap: 44,
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
        smooth: false,
        showSymbol: false,
        data: chartData.value.apiCalls,
        lineStyle: { width: 2, color: dark ? '#5EEAD4' : '#0F766E' },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(15,118,110,0.15)' },
            { offset: 1, color: 'rgba(15,118,110,0)' },
          ]),
        },
      },
      {
        name: '令牌数',
        type: 'line',
        yAxisIndex: 1,
        smooth: false,
        showSymbol: false,
        data: chartData.value.tokens,
        lineStyle: { width: 2, color: '#609AC5' },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(96,154,197,0.10)' },
            { offset: 1, color: 'rgba(96,154,197,0)' },
          ]),
        },
      },
    ],
  }
}

function refreshChart() {
  if (!chart) return
  const series = props.series || dashboardStore.timeSeriesData
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
  () => [props.series, dashboardStore.timeSeriesData.calls, dashboardStore.timeSeriesData.tokens],
  () => {
    nextTick(refreshChart)
  }
)

watch(isDark, () => {
  nextTick(refreshChart)
})

onMounted(() => {
  initChart()
  if (typeof ResizeObserver !== 'undefined') {
    resizeObserver = new ResizeObserver(handleResize)
    resizeObserver.observe(chartContainer.value)
  }
  window.addEventListener('resize', handleResize)
})

onUnmounted(() => {
  window.removeEventListener('resize', handleResize)
  resizeObserver?.disconnect()
  if (chart) {
    chart.dispose()
    chart = null
  }
})
</script>

<template>
  <div><p class="section-description" style="padding:8px 16px 0">{{ caption }}</p><div ref="chartContainer" class="chart" role="img" aria-label="最近30分钟上游完成次数和Token趋势"></div></div>
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
