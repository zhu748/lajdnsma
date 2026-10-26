// Validate the entire draft before the first network write. Unchanged legacy
// values do not prevent saving an unrelated setting.
export function validateNumericChanges(draft, current, rules) {
  for (const [key, label, minimum, integer = true] of rules) {
    const value = draft[key]
    if (value === current[key]) continue
    if (typeof value !== 'number' || !Number.isFinite(value) ||
        value < minimum || (integer && !Number.isInteger(value))) {
      throw new Error(`${label}必须为不小于 ${minimum} 的${integer ? '整数' : '数字'}`)
    }
  }
}
