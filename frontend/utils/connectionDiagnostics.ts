/** Compare wrapper messages without discarding their original diagnostics. */
export function sameConnectionDiagnostic(a?: string | null, b?: string | null): boolean {
  if (!a || !b) return false
  const normalize = (value: string) => value.toLowerCase().replace(/\s+/g, ' ').trim()
  const left = normalize(a), right = normalize(b)
  return left === right || (Math.min(left.length, right.length) >= 24 && (left.includes(right) || right.includes(left)))
}
