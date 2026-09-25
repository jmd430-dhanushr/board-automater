import { useState } from 'react'
import { testConnection } from '../../utils/api'

function Field({ label, hint, children }) {
  return (
    <div className="field">
      <label className="field-label">
        {label}
        {hint && <span>{hint}</span>}
      </label>
      {children}
    </div>
  )
}

export default function Connection({ jira, setJira, azure, setAzure, mapping, onSave, onReset }) {
  const [status, setStatus] = useState(null)
  const [testing, setTesting] = useState(false)

  async function handleTest() {
    setTesting(true)
    setStatus({ cls: '', msg: '⏳ Testing connection…' })
    try {
      const res = await testConnection({ jira, azure, mapping })
      if (res.all_ok) {
        setStatus({ cls: 'ok', msg: `✅ Jira OK — ${res.jira_user}    ✅ Azure OK — ${res.azure_project}` })
      } else {
        const lines = [
          res.jira_ok  ? '✅ Jira OK'  : '❌ Jira FAILED',
          res.azure_ok ? '✅ Azure OK' : '❌ Azure FAILED',
          ...(res.errors || []),
        ]
        setStatus({ cls: 'error', msg: lines.join('\n') })
      }
    } catch (e) {
      setStatus({ cls: 'error', msg: `❌ Request failed: ${e.message}` })
    }
    setTesting(false)
  }

  function handleSave() {
    onSave()
    setStatus({ cls: 'ok', msg: '✓ Configuration saved to browser storage.' })
  }

  function handleReset() {
    onReset()
    setStatus({ cls: '', msg: 'Fields reset to defaults. Enter your API tokens and save.' })
  }

  return (
    <div>
      <div className="grid-2">
        <div className="form-group">
          <div className="field-group-label">Jira</div>
          <Field label="Base URL">
            <input className="field-input" type="url"
              value={jira.base_url}
              onChange={e => setJira(j => ({ ...j, base_url: e.target.value.trim().replace(/\/+$/, '') }))}
              placeholder="https://your-domain.atlassian.net" />
          </Field>
          <Field label="Email">
            <input className="field-input" type="email"
              value={jira.email}
              onChange={e => setJira(j => ({ ...j, email: e.target.value.trim() }))}
              placeholder="you@company.com" />
          </Field>
          <Field label="API Token" hint={<a href="https://id.atlassian.com/manage-profile/security/api-tokens" target="_blank" rel="noreferrer">generate →</a>}>
            <input className="field-input" type="password"
              value={jira.api_token}
              onChange={e => setJira(j => ({ ...j, api_token: e.target.value }))}
              placeholder="Atlassian API token" autoComplete="new-password" />
          </Field>
          <Field label="Project Key">
            <input className="field-input" type="text"
              value={jira.project_key}
              onChange={e => setJira(j => ({ ...j, project_key: e.target.value.trim() }))}
              placeholder="PROJ" />
          </Field>
        </div>

        <div className="form-group">
          <div className="field-group-label">Azure DevOps</div>
          <Field label="Organisation">
            <input className="field-input" type="text"
              value={azure.org}
              onChange={e => setAzure(a => ({ ...a, org: e.target.value.trim() }))}
              placeholder="your-organisation" />
          </Field>
          <Field label="Project">
            <input className="field-input" type="text"
              value={azure.project}
              onChange={e => setAzure(a => ({ ...a, project: e.target.value.trim() }))}
              placeholder="your-project" />
          </Field>
          <Field label="Team">
            <input className="field-input" type="text"
              value={azure.team}
              onChange={e => setAzure(a => ({ ...a, team: e.target.value.trim() }))}
              placeholder="Your Team" />
          </Field>
          <Field label="Personal Access Token" hint="Work Items: Read &amp; Write">
            <input className="field-input" type="password"
              value={azure.pat}
              onChange={e => setAzure(a => ({ ...a, pat: e.target.value }))}
              placeholder="Azure DevOps PAT" autoComplete="new-password" />
          </Field>
        </div>
      </div>

      <div className="btn-row">
        <button className="btn btn-primary" onClick={handleTest} disabled={testing}>
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M8 2a6 6 0 1 0 6 6"/><path d="M11 2l3 3-3 3"/>
          </svg>
          {testing ? 'Testing…' : 'Test Connection'}
        </button>
        <button className="btn btn-secondary" onClick={handleSave}>
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M3 14V5.5L5.5 3H13v11H3z"/><path d="M6 14v-4h4v4"/><path d="M6 3v3h5"/>
          </svg>
          Save
        </button>
        <button className="btn btn-ghost btn-sm" onClick={handleReset}>Reset</button>
      </div>

      {status && (
        <div className={`result${status.cls ? ' ' + status.cls : ''}`}
          style={{ whiteSpace: 'pre-line' }}>
          {status.msg}
        </div>
      )}
    </div>
  )
}
