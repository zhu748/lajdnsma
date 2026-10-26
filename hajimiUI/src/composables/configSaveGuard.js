import { onBeforeUnmount } from 'vue'

// A batch must stop after logout, session replacement, or panel removal.
export function useConfigSaveGuard(store) {
  let disposed = false
  onBeforeUnmount(() => { disposed = true })
  return () => {
    const assertSession = store.captureSession()
    return () => {
      assertSession()
      if (disposed) throw new Error('配置面板已关闭，后续保存已停止')
    }
  }
}
