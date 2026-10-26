import { ref } from 'vue'
// Refresh untouched fields while preserving edits made since the last server value.
export function createConfigDraft(local) {
  let baseline = null
  const revision = ref(0)
  const copy = value => JSON.parse(JSON.stringify(value))
  const equal = (a, b) => JSON.stringify(a) === JSON.stringify(b)
  function sync(source) {
    for (const [key, value] of Object.entries(source)) {
      if (!baseline || equal(local[key], baseline[key])) local[key] = copy(value)
    }
    baseline = copy(source)
    revision.value += 1
  }
  // A successful submission changes the server baseline before the next poll.
  // This also preserves edits that deliberately return to the old server value
  // while a different submitted value is still in flight.
  sync.acknowledge = (key, value) => {
    baseline ||= {}
    baseline[key] = copy(value)
    revision.value += 1
  }
  sync.isDirty = () => { revision.value; return baseline != null && Object.keys(baseline).some(key => !equal(local[key], baseline[key])) }
  return sync
}
