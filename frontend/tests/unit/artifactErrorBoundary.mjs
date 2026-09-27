// Run the iframe error boundary script in a sandboxed context with a fake
// window/React/ReactDOM. App-data rejections are tagged `__bowAppDataError` by
// the runtime and are expected, handled failures: they must never hide the
// dashboard behind an ARTIFACT_ERROR. Any other unhandled rejection still must.
import assert from 'node:assert/strict'
import vm from 'node:vm'

import * as iframe from '../../utils/artifactIframe.ts'

assert.equal(typeof iframe.errorBoundaryScript, 'function', 'errorBoundaryScript must be exported for testing')

function boot() {
  const posted = []
  const listeners = {}
  const window = {
    parent: { postMessage(msg, target) { posted.push({ msg, target }) } },
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn) },
  }
  const React = { Component: class {}, createElement: (type, props, child) => ({ type, props, child }) }
  const ReactDOM = { render() {} }
  const ctx = vm.createContext({ window, React, ReactDOM })
  vm.runInContext(iframe.errorBoundaryScript(), ctx)
  const reject = (reason) => {
    const event = { reason, defaultPrevented: false, preventDefault() { this.defaultPrevented = true } }
    for (const fn of listeners.unhandledrejection || []) fn(event)
    return event
  }
  return { posted, reject, listeners }
}

// A tagged app-data rejection is ignored by the boundary.
{
  const { posted, reject, listeners } = boot()
  assert.equal((listeners.unhandledrejection || []).length, 1, 'boundary registers one unhandledrejection listener')
  const reason = Object.assign(new Error('Not allowed for this collection'), { code: 'forbidden', __bowAppDataError: true })
  reject(reason)
  assert.deepEqual(posted, [], 'tagged app-data rejection must not post ARTIFACT_ERROR')
  // The flag stays clear, so a later real failure is still reported.
  reject(new Error('boom after app data'))
  assert.equal(posted.length, 1)
  assert.equal(posted[0].msg.payload.message, 'boom after app data')
}

// An untagged rejection keeps the existing behavior: exactly one ARTIFACT_ERROR.
{
  const { posted, reject } = boot()
  reject(new Error('fetch exploded'))
  reject(new Error('second failure is deduplicated'))
  assert.equal(posted.length, 1)
  assert.deepEqual(JSON.parse(JSON.stringify(posted[0].msg)), { type: 'ARTIFACT_ERROR', payload: { message: 'fetch exploded' } })
  assert.equal(posted[0].target, '*')
}

// A non-object reason (plain string) is still reported, and a falsy tag is not a pass.
{
  const { posted, reject } = boot()
  reject({ message: 'tag is false', __bowAppDataError: false })
  assert.equal(posted.length, 1)
  assert.equal(posted[0].msg.payload.message, 'tag is false')
}
{
  const { posted, reject } = boot()
  reject('plain string reason')
  assert.equal(posted.length, 1)
  assert.equal(posted[0].msg.payload.message, 'plain string reason')
}

console.log('artifactErrorBoundary: all assertions passed')
