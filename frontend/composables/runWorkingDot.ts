/**
 * The run-activity dot on /reports/:id: one circle at the tail of the
 * assistant message for the whole time the run works.
 *
 * It used to be two dots with separate rules — a 12px one above the blocks
 * before the first tool step, and a 10px pulsing one below them after it —
 * and both hid whenever the newest block held text or a visible tool was
 * running. So the circle vanished during every silence after the run had
 * said something (a planner round, code generation, the gap before the
 * answer), changed size and jumped from top to bottom mid-run.
 *
 * Now it shows for every live frame, whatever the blocks look like, and only
 * goes away when the run ends, is being stopped, or is waiting on the user
 * (a confirmation card: the run is not working, the user is).
 */
export interface RunWorkingMessage {
  role?: string
  status?: string
  sigkill?: string | null
}

export function isRunWorking<B>(
  message: RunWorkingMessage,
  blocks: B[],
  isAwaitingUser: (block: B) => boolean,
): boolean {
  if (message.role !== 'system' || message.status !== 'in_progress' || message.sigkill) return false
  return !blocks.some(isAwaitingUser)
}
