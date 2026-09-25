async function errorMessage(r) {
  if (r.status === 404) {
    return 'Board Automater backend not found (HTTP 404). Make sure it is running and that nothing else is using its port.'
  }
  try {
    const body = await r.json()
    if (typeof body.detail === 'string') return body.detail
    if (Array.isArray(body.detail)) return body.detail.map(d => d.msg).join('; ')
  } catch {}
  return `Server returned HTTP ${r.status}`
}

async function post(url, body) {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!r.ok) throw new Error(await errorMessage(r))
  return r.json()
}

export async function getDefaultConfig() {
  const r = await fetch('/api/default-config')
  if (!r.ok) throw new Error(await errorMessage(r))
  return r.json()
}

export async function testConnection(payload) {
  return post('/api/test-connection', payload)
}

// Runs a sync and calls onEvent for every progress event the server streams
// (newline-delimited JSON), as soon as it arrives.
export async function streamSync(dryRun, payload, onEvent) {
  const r = await fetch(`/api/sync/stream?dry_run=${dryRun}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!r.ok) throw new Error(await errorMessage(r))

  const reader  = r.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let nl
    while ((nl = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, nl).trim()
      buffer = buffer.slice(nl + 1)
      if (line) onEvent(JSON.parse(line))
    }
  }
  if (buffer.trim()) onEvent(JSON.parse(buffer))
}
