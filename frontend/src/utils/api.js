async function post(url, body) {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return r.json()
}

export async function getDefaultConfig() {
  const r = await fetch('/api/default-config')
  return r.json()
}

export async function testConnection(payload) {
  return post('/api/test-connection', payload)
}

export async function syncApi(dryRun, payload) {
  return post(dryRun ? '/api/sync/preview' : '/api/sync/run', payload)
}
