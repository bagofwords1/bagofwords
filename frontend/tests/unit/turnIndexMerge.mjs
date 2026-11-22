import assert from 'node:assert/strict'

import { mergeNavTurns } from '../../utils/turnIndex.ts'

// The conversation map lists every prompt of the report (the server turn
// index), not just the pages loaded in the transcript; loaded turns supply the
// live text. Contract: index order + membership, loaded text wins, turns the
// index doesn't know yet still show up in place.

const t = (id, prompt = `p${id}`, answer = `a${id}`) => ({ id, prompt, answer })
const ids = (list) => list.map(x => x.id)

// Long report, only the newest page loaded: the map still starts at turn 1.
{
  const index = Array.from({ length: 40 }, (_, i) => t(String(i), `preview ${i}`, `ans ${i}`))
  const loaded = [t('35', 'full prompt 35', 'full answer 35'), t('36'), t('37'), t('38'), t('39')]
  const merged = mergeNavTurns(index, loaded)
  assert.deepEqual(ids(merged), ids(index), 'one tick per prompt of the whole report, in order')
  assert.equal(merged[0].prompt, 'preview 0', 'unloaded turns use the index preview')
  assert.equal(merged[35].prompt, 'full prompt 35', 'a loaded turn shows its full prompt')
  assert.equal(merged[35].answer, 'full answer 35')
}

// A streaming turn with no answer text yet keeps the index preview; a loaded
// answer replaces it as soon as it has text.
{
  const merged = mergeNavTurns([t('a', 'q', 'old preview')], [t('a', 'q', '')])
  assert.equal(merged[0].answer, 'old preview')
}

// A prompt sent after the index was fetched appears right away, after the
// loaded turn before it — not at the end of the list and not dropped.
{
  const index = [t('1'), t('2'), t('3')]
  const loaded = [t('2'), t('new-a'), t('3'), t('new-b')]
  assert.deepEqual(ids(mergeNavTurns(index, loaded)), ['1', '2', 'new-a', '3', 'new-b'])
}

// New turn with no known predecessor among loaded turns goes first in the
// loaded window's position (before the first index entry it precedes).
{
  assert.deepEqual(ids(mergeNavTurns([t('1'), t('2')], [t('x'), t('2')])), ['x', '1', '2'])
}

// No index (not fetched yet / failed): fall back to the loaded turns unchanged.
{
  const loaded = [t('7'), t('8')]
  assert.deepEqual(mergeNavTurns([], loaded), loaded)
}

// Inputs are not mutated.
{
  const index = [t('1', 'p', 'a')]
  const loaded = [t('1', 'P', 'A')]
  mergeNavTurns(index, loaded)
  assert.deepEqual(index, [t('1', 'p', 'a')])
}

console.log('turnIndexMerge: ok')
