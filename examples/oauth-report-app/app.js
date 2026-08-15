const byId = (id) => document.getElementById(id)
const redirectUri = `${window.location.origin}/callback`

const state = {
  accessToken: sessionStorage.getItem('northstar.accessToken') || '',
  refreshToken: sessionStorage.getItem('northstar.refreshToken') || '',
  clientId: localStorage.getItem('northstar.clientId') || '',
  bowOrigin: localStorage.getItem('northstar.bowOrigin') || 'http://127.0.0.1:3001',
  activeReport: null,
  completionId: null,
  running: false,
  events: 0,
  tools: new Map(),
  answerBlocks: new Map(),
}

const elements = {
  activityFeed: byId('activityFeed'),
  activeReportTitle: byId('activeReportTitle'),
  bowOrigin: byId('bowOrigin'),
  clientId: byId('clientId'),
  connectButton: byId('connectButton'),
  connectPanel: byId('connectPanel'),
  connectionForm: byId('connectionForm'),
  eventCount: byId('eventCount'),
  identityPill: byId('identityPill'),
  logoutButton: byId('logoutButton'),
  promptForm: byId('promptForm'),
  promptInput: byId('promptInput'),
  refreshReports: byId('refreshReports'),
  reportCount: byId('reportCount'),
  reportList: byId('reportList'),
  runButton: byId('runButton'),
  streamStatus: byId('streamStatus'),
  timeline: byId('timeline'),
  toast: byId('toast'),
  workspace: byId('workspace'),
}

elements.clientId.value = state.clientId
elements.bowOrigin.value = state.bowOrigin

function notify(message) {
  elements.toast.textContent = message
  elements.toast.classList.add('show')
  window.setTimeout(() => elements.toast.classList.remove('show'), 3200)
}

function persistTokens(payload) {
  state.accessToken = payload.access_token || ''
  state.refreshToken = payload.refresh_token || ''
  sessionStorage.setItem('northstar.accessToken', state.accessToken)
  sessionStorage.setItem('northstar.refreshToken', state.refreshToken)
}

function clearTokens() {
  state.accessToken = ''
  state.refreshToken = ''
  sessionStorage.removeItem('northstar.accessToken')
  sessionStorage.removeItem('northstar.refreshToken')
}

function base64Url(bytes) {
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

async function createPkce() {
  const verifier = base64Url(crypto.getRandomValues(new Uint8Array(64)))
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))
  return { verifier, challenge: base64Url(new Uint8Array(digest)) }
}

async function beginLogin() {
  const clientId = elements.clientId.value.trim()
  const bowOrigin = elements.bowOrigin.value.trim().replace(/\/$/, '')
  if (!clientId || !bowOrigin) return
  state.clientId = clientId
  state.bowOrigin = bowOrigin
  localStorage.setItem('northstar.clientId', clientId)
  localStorage.setItem('northstar.bowOrigin', bowOrigin)

  const pkce = await createPkce()
  const oauthState = base64Url(crypto.getRandomValues(new Uint8Array(24)))
  sessionStorage.setItem('northstar.pkceVerifier', pkce.verifier)
  sessionStorage.setItem('northstar.oauthState', oauthState)

  const authorize = new URL('/api/oauth/authorize', bowOrigin)
  authorize.searchParams.set('response_type', 'code')
  authorize.searchParams.set('client_id', clientId)
  authorize.searchParams.set('redirect_uri', redirectUri)
  authorize.searchParams.set('scope', 'app')
  authorize.searchParams.set('state', oauthState)
  authorize.searchParams.set('code_challenge', pkce.challenge)
  authorize.searchParams.set('code_challenge_method', 'S256')
  window.location.assign(authorize)
}

async function exchangeCallback() {
  const query = new URLSearchParams(window.location.search)
  const code = query.get('code')
  const oauthError = query.get('error')
  if (!code && !oauthError) return false
  const returnedState = query.get('state')
  const expectedState = sessionStorage.getItem('northstar.oauthState')
  if (!returnedState || returnedState !== expectedState) throw new Error('OAuth state did not match')

  if (oauthError) {
    sessionStorage.removeItem('northstar.pkceVerifier')
    sessionStorage.removeItem('northstar.oauthState')
    window.history.replaceState({}, '', '/')
    if (oauthError === 'access_denied') {
      throw new Error('Access was not granted. Nothing was shared with Northstar.')
    }
    throw new Error(query.get('error_description') || oauthError)
  }

  const verifier = sessionStorage.getItem('northstar.pkceVerifier')
  const form = new URLSearchParams({
    grant_type: 'authorization_code',
    code,
    client_id: state.clientId,
    redirect_uri: redirectUri,
    code_verifier: verifier || '',
  })
  const response = await fetch('/bow/api/oauth/token', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: form,
  })
  const body = await response.json()
  if (!response.ok) throw new Error(body.error_description || body.detail || 'Token exchange failed')
  persistTokens(body)
  sessionStorage.removeItem('northstar.pkceVerifier')
  sessionStorage.removeItem('northstar.oauthState')
  window.history.replaceState({}, '', '/')
  return true
}

async function refreshAccessToken() {
  if (!state.refreshToken || !state.clientId) return false
  const response = await fetch('/bow/api/oauth/token', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      grant_type: 'refresh_token',
      refresh_token: state.refreshToken,
      client_id: state.clientId,
    }),
  })
  if (!response.ok) return false
  persistTokens(await response.json())
  return true
}

async function bowFetch(path, options = {}, retry = true) {
  const headers = new Headers(options.headers || {})
  headers.set('Authorization', `Bearer ${state.accessToken}`)
  if (options.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  const response = await fetch(`/bow/api${path}`, { ...options, headers })
  if (response.status === 401 && retry && await refreshAccessToken()) return bowFetch(path, options, false)
  return response
}

async function jsonFetch(path, options) {
  const response = await bowFetch(path, options)
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(body.detail || body.error_description || `BOW returned ${response.status}`)
  return body
}

function setConnected(user) {
  elements.connectPanel.classList.add('hidden')
  elements.workspace.classList.remove('hidden')
  elements.connectButton.classList.add('hidden')
  elements.logoutButton.classList.remove('hidden')
  elements.identityPill.classList.remove('hidden')
  elements.identityPill.textContent = user.name || user.email || 'Connected to BOW'
}

function setDisconnected() {
  elements.connectPanel.classList.remove('hidden')
  elements.workspace.classList.add('hidden')
  elements.connectButton.classList.remove('hidden')
  elements.logoutButton.classList.add('hidden')
  elements.identityPill.classList.add('hidden')
}

function formatAge(value) {
  if (!value) return 'Recently updated'
  const minutes = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 60000))
  if (minutes < 1) return 'Just now'
  if (minutes < 60) return `${minutes}m ago`
  if (minutes < 1440) return `${Math.round(minutes / 60)}h ago`
  return `${Math.round(minutes / 1440)}d ago`
}

async function loadReports() {
  const body = await jsonFetch('/reports?filter=my&limit=20&view=minimal')
  const reports = (body.reports || []).filter((report) => (report.title || '').startsWith('Northstar analysis'))
  elements.reportCount.textContent = String(reports.length)
  elements.reportList.replaceChildren()
  if (!reports.length) {
    elements.reportList.innerHTML = '<p class="empty-list">Your first streamed report will appear here.</p>'
    return
  }
  reports.forEach((report) => {
    const button = document.createElement('button')
    button.className = `report-button${state.activeReport?.id === report.id ? ' active' : ''}`
    button.type = 'button'
    button.innerHTML = `<strong></strong><span></span>`
    button.querySelector('strong').textContent = report.title || 'Untitled report'
    button.querySelector('span').textContent = formatAge(report.last_activity_at || report.updated_at)
    button.addEventListener('click', () => selectReport(report))
    elements.reportList.append(button)
  })
}

function clearTimeline() {
  elements.timeline.replaceChildren()
  state.tools.clear()
  state.answerBlocks.clear()
}

function selectReport(report) {
  state.activeReport = report
  elements.activeReportTitle.textContent = report.title || 'Untitled report'
  clearTimeline()
  const message = document.createElement('div')
  message.className = 'message assistant'
  message.innerHTML = '<div class="message-label">Northstar</div><div class="assistant-copy">This BOW report is ready for a new streamed analysis.</div>'
  elements.timeline.append(message)
  loadReports().catch(() => {})
}

async function ensureReport() {
  if (state.activeReport) return state.activeReport
  const report = await jsonFetch('/reports', {
    method: 'POST',
    body: JSON.stringify({
      title: `Northstar analysis · ${new Date().toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}`,
      files: [],
      data_sources: [],
    }),
  })
  selectReport(report)
  await loadReports()
  return report
}

function appendUserMessage(prompt) {
  const message = document.createElement('div')
  message.className = 'message user'
  message.textContent = prompt
  elements.timeline.append(message)
}

function eventDescription(event, data) {
  if (event === 'completion.started') return `Model: ${data.model || 'organization default'}`
  if (event === 'tool.started') return `${data.tool_name || 'tool'} started`
  if (event === 'tool.finished') return data.result_summary || `${data.tool_name || 'tool'} ${data.status || 'finished'}`
  if (event === 'completion.finished') return 'Stream completed successfully'
  if (event === 'completion.error') return data.error || 'Completion failed'
  if (event.startsWith('block.delta')) return data.field ? `Streaming ${data.field}` : 'Artifact updated'
  if (event === 'block.upsert') return data.block?.title || 'Response block updated'
  return event.replaceAll('.', ' ')
}

function compactSummary(value, limit = 260) {
  const normalized = String(value || '')
    .split(/\n\s*<agents>|\n\s*<instructions>/i)[0]
    .replace(/\s+/g, ' ')
    .trim()
  return normalized.length > limit ? `${normalized.slice(0, limit - 1).trimEnd()}…` : normalized
}

function appendActivity(event, data) {
  if (event === 'block.delta.token') return
  const empty = elements.activityFeed.querySelector('.activity-empty')
  if (empty) empty.remove()
  state.events += 1
  elements.eventCount.textContent = `${state.events} event${state.events === 1 ? '' : 's'}`
  const row = document.createElement('div')
  row.className = 'activity-event'
  const icon = event.includes('tool') ? '⚙' : event.includes('error') ? '!' : event.includes('finished') ? '✓' : '⌁'
  row.innerHTML = '<span class="event-icon"></span><div class="event-copy"><strong></strong><span></span></div><time class="event-time"></time>'
  row.querySelector('.event-icon').textContent = icon
  row.querySelector('strong').textContent = event
  row.querySelector('.event-copy span').textContent = eventDescription(event, data)
  row.querySelector('time').textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  elements.activityFeed.append(row)
  elements.activityFeed.scrollTop = elements.activityFeed.scrollHeight
}

function answerBlock(blockId = 'main') {
  if (state.answerBlocks.has(blockId)) return state.answerBlocks.get(blockId)
  const message = document.createElement('div')
  message.className = 'message assistant'
  message.innerHTML = '<div class="message-label">Northstar · streaming</div><div class="assistant-copy"></div>'
  elements.timeline.append(message)
  const copy = message.querySelector('.assistant-copy')
  state.answerBlocks.set(blockId, copy)
  return copy
}

function toolIcon(name) {
  if (name === 'clarify') return '?'
  if (name === 'create_data') return '⌁'
  if (name === 'create_artifact') return '◇'
  return '⚙'
}

function getToolCard(data) {
  const id = data.tool_execution_id || data.block_id || `${data.tool_name}-${state.tools.size}`
  if (state.tools.has(id)) return state.tools.get(id)
  const card = document.createElement('div')
  card.className = 'tool-card running'
  card.dataset.toolId = id
  card.innerHTML = '<div class="tool-top"><div class="tool-name"><span class="tool-icon"></span><span class="tool-title"></span></div><span class="tool-state">Running</span></div><p class="tool-summary"></p>'
  card.querySelector('.tool-icon').textContent = toolIcon(data.tool_name)
  card.querySelector('.tool-title').textContent = (data.tool_name || 'tool').replaceAll('_', ' ')
  card.querySelector('.tool-summary').textContent = compactSummary(data.arguments?.context) || 'BOW is executing this step…'
  elements.timeline.append(card)
  state.tools.set(id, card)
  return card
}

function renderClarify(card, data) {
  const questions = data.arguments?.questions || card._toolArguments?.questions || data.result_json?.questions || []
  if (!questions.length || card.querySelector('.clarify-form')) return
  const form = document.createElement('form')
  form.className = 'clarify-form'
  questions.forEach((question, index) => {
    const wrapper = document.createElement('div')
    wrapper.className = 'question'
    const label = document.createElement('label')
    label.textContent = question.text
    wrapper.append(label)
    if (question.options?.length) {
      const options = document.createElement('div')
      options.className = 'options'
      question.options.forEach((value) => {
        const button = document.createElement('button')
        button.type = 'button'
        button.className = 'option'
        button.textContent = value
        button.dataset.value = value
        button.addEventListener('click', () => {
          if (!question.multi_select) options.querySelectorAll('.option').forEach((item) => item.classList.remove('selected'))
          button.classList.toggle('selected')
        })
        options.append(button)
      })
      wrapper.append(options)
    } else {
      const input = document.createElement('input')
      input.className = 'clarify-input'
      input.name = `question-${index}`
      input.placeholder = 'Type your answer…'
      wrapper.append(input)
    }
    form.append(wrapper)
  })
  const submit = document.createElement('button')
  submit.className = 'button primary clarify-submit'
  submit.type = 'submit'
  submit.textContent = 'Submit answers'
  form.append(submit)
  form.addEventListener('submit', async (event) => {
    event.preventDefault()
    const selectedChips = []
    const otherTexts = []
    const freeTexts = []
    const promptLines = []
    questions.forEach((question, index) => {
      const wrapper = form.querySelectorAll('.question')[index]
      let answer = ''
      if (question.options?.length) {
        const values = [...wrapper.querySelectorAll('.option.selected')].map((item) => item.dataset.value)
        selectedChips.push(question.multi_select ? values : (values[0] || ''))
        otherTexts.push('')
        freeTexts.push('')
        answer = values.join(', ')
      } else {
        const value = wrapper.querySelector('input').value.trim()
        selectedChips.push('')
        otherTexts.push('')
        freeTexts.push(value)
        answer = value
      }
      promptLines.push(`Q: ${question.text}\nA: ${answer}`)
    })
    if (promptLines.some((line) => line.endsWith('A: '))) {
      notify('Please answer every question.')
      return
    }
    submit.disabled = true
    await jsonFetch(`/completions/${state.completionId}/tool_executions/${data.tool_execution_id}/clarify_response`, {
      method: 'POST',
      body: JSON.stringify({ selected_chips: selectedChips, other_texts: otherTexts, free_texts: freeTexts }),
    })
    form.querySelectorAll('button, input').forEach((control) => { control.disabled = true })
    elements.promptInput.value = promptLines.join('\n\n')
    await runPrompt(elements.promptInput.value)
  })
  card.append(form)
}

function handleEvent(event, payload) {
  const data = payload.data || {}
  appendActivity(event, data)
  if (data.system_completion_id) {
    state.completionId = data.system_completion_id
  } else if (payload.completion_id && event !== 'completion.finished') {
    state.completionId = payload.completion_id
  }

  if (event === 'block.delta.token' && data.field === 'content') {
    const copy = answerBlock(data.block_id)
    copy.textContent += data.token || ''
  } else if (event === 'block.delta.text' && data.field === 'content') {
    answerBlock(data.block_id).textContent = data.text || ''
  } else if (event === 'block.upsert' && data.block?.content) {
    answerBlock(data.block.id).textContent = data.block.content
  } else if (event === 'decision.partial' && data.assistant) {
    answerBlock('decision').textContent = data.assistant
  } else if (event === 'tool.started') {
    const card = getToolCard(data)
    card._toolArguments = data.arguments || {}
  } else if (event === 'tool.finished') {
    const card = getToolCard(data)
    card.classList.remove('running')
    card.classList.add(data.status === 'success' ? 'success' : 'error')
    card.querySelector('.tool-state').textContent = data.status || 'Finished'
    card.querySelector('.tool-summary').textContent = compactSummary(data.result_summary) || 'Step complete'
    if (data.tool_name === 'clarify') renderClarify(card, data)
  } else if (event === 'completion.error') {
    setRunState('error', 'Error')
    answerBlock('error').textContent = data.error || 'The completion failed.'
  }
  elements.timeline.scrollTop = elements.timeline.scrollHeight
}

function setRunState(kind, label) {
  elements.streamStatus.className = `stream-status ${kind}`
  elements.streamStatus.innerHTML = `<i></i>${label}`
}

function parseSseChunk(buffer, onEvent) {
  const records = buffer.split(/\n\n/)
  const tail = records.pop() || ''
  records.forEach((record) => {
    if (!record.trim() || record.startsWith(':')) return
    let event = 'message'
    const dataLines = []
    record.split(/\r?\n/).forEach((line) => {
      if (line.startsWith('event:')) event = line.slice(6).trim()
      if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart())
    })
    const raw = dataLines.join('\n')
    if (!raw || raw === '[DONE]') return
    try { onEvent(event, JSON.parse(raw)) } catch { /* ignore malformed frames */ }
  })
  return tail
}

async function runPrompt(prompt) {
  if (state.running || !prompt.trim()) return
  state.running = true
  elements.runButton.disabled = true
  setRunState('running', 'Streaming')
  const report = await ensureReport()
  appendUserMessage(prompt.trim())
  elements.promptInput.value = ''
  state.answerBlocks.clear()

  try {
    const response = await bowFetch(`/reports/${report.id}/completions?stream=true`, {
      method: 'POST',
      headers: { Accept: 'text/event-stream' },
      body: JSON.stringify({ prompt: { content: prompt.trim(), mode: 'chat' }, stream: true }),
    })
    if (!response.ok || !response.body) {
      const body = await response.json().catch(() => ({}))
      throw new Error(body.detail || `BOW returned ${response.status}`)
    }
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    while (true) {
      const { done, value } = await reader.read()
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done }).replace(/\r\n/g, '\n')
      buffer = parseSseChunk(buffer, handleEvent)
      if (done) break
    }
    setRunState('ready', 'Complete')
    notify('BOW finished the streamed analysis.')
    await loadReports()
  } catch (error) {
    setRunState('error', 'Error')
    notify(error instanceof Error ? error.message : String(error))
  } finally {
    state.running = false
    elements.runButton.disabled = false
  }
}

async function initialize() {
  try {
    const returned = await exchangeCallback()
    if (returned) notify('Securely connected to BOW.')
    if (!state.accessToken) {
      setDisconnected()
      return
    }
    const user = await jsonFetch('/users/whoami')
    setConnected(user)
    await loadReports()
  } catch (error) {
    clearTokens()
    setDisconnected()
    notify(error instanceof Error ? error.message : String(error))
  }
}

elements.connectionForm.addEventListener('submit', (event) => { event.preventDefault(); beginLogin() })
elements.connectButton.addEventListener('click', () => elements.connectPanel.scrollIntoView({ behavior: 'smooth' }))
elements.logoutButton.addEventListener('click', () => { clearTokens(); state.activeReport = null; setDisconnected(); notify('Disconnected from BOW.') })
elements.promptForm.addEventListener('submit', (event) => { event.preventDefault(); runPrompt(elements.promptInput.value) })
elements.refreshReports.addEventListener('click', () => loadReports().then(() => notify('Reports refreshed.')).catch((error) => notify(error.message)))
document.querySelectorAll('.starter').forEach((button) => button.addEventListener('click', () => { elements.promptInput.value = button.dataset.prompt; elements.promptInput.focus() }))

initialize()
