// Confirmation ids the user has already answered in this page session.
// A tool's progress_stage stays 'awaiting_confirmation' until the backend
// streams its next event, so the prompt box indicator consults this set to
// flip back to "Working" the moment the user clicks approve/reject.
const answered = reactive(new Set<string>())

export function useToolConfirmations() {
  const markConfirmationAnswered = (id?: string | null) => { if (id) answered.add(id) }
  const isConfirmationAnswered = (id?: string | null) => !!id && answered.has(id)
  return { markConfirmationAnswered, isConfirmationAnswered }
}
