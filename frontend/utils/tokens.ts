// Cheap token estimate (~4 chars per token). Mirrors the backend's default
// `estimate_tokens_fast` in backend/app/ai/utils/token_counter.py so the number
// shown while editing matches what the agent budgets for.
export function estimateTokens(text: string | null | undefined): number {
  if (!text) return 0
  return Math.max(1, Math.ceil(text.length / 4))
}

// 950 -> "950", 1234 -> "1.2K", 2_500_000 -> "2.5M"
export function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, '')}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1).replace(/\.0$/, '')}K`
  return String(n)
}
