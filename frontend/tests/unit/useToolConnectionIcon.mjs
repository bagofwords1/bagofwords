import assert from 'node:assert/strict'

import { useToolConnectionIcon } from '../../composables/useToolConnectionIcon.ts'

const dataSources = [
  {
    id: 'agent-1',
    name: 'Mail agent',
    connections: [
      {
        id: 'connection-1',
        name: 'Gmail',
        type: 'gmail_mail',
      },
    ],
  },
]

const toolExecution = {
  arguments_json: { connection_id: 'connection-1' },
  result_json: {},
}

const icon = useToolConnectionIcon(
  () => toolExecution,
  () => dataSources,
  { connectionTypes: ['gmail_mail'] },
)

assert.deepEqual(icon.value, {
  type: 'gmail_mail',
  connectorKey: null,
})

console.log('getter inputs resolve to the matching connection icon')
