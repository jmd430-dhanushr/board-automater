import { useState } from 'react'
import { computeRange } from '../../utils/dates'
import { syncApi } from '../../utils/api'
import SyncResultPanel from '../SyncResultPanel'

const PRESETS = [
  { id: 'today',     label: 'Today' },
  { id: 'this-week', label: 'This Week' },
  { id: 'last-week', label: 'Last Week' },
  { id: '7d',        label: 'Last 7 Days' },
  { id: '30d',       label: 'Last 30 Days' },
  { id: 'custom',    label: 'Custom…' },
]

const FIELD_LABELS = {
  updated: 'last updated',
  created: 'created',
  either:  'created or updated',
}

function DateSummary({ from, to, field }) {
  const fLabel = FIELD_LABELS[field] || 'updated'
  if (from && to) return <>Only tickets <strong>{fLabel}</strong> between <strong>{from}</strong> and <strong>{to}</strong> will be synced.</>
  if (from)       return <>Only tickets <strong>{fLabel}</strong> on or after <strong>{from}</strong> will be synced.</>
  return              <>Only tickets <strong>{fLabel}</strong> on or before <strong>{to}</strong> will be synced.</>
}

export default function RunSync({ jira, azure, mapping, dateFilter, setDateFilter }) {
  const [syncing,    setSyncing]    = useState(false)
  const [syncResult, setSyncResult] = useState(null)
  const [error,      setError]      = useState(null)

  const { preset, from, to, field } = dateFilter
  const hasRange = !!(from || to)

  function setPreset(p) {
    if (p === 'custom') {
      setDateFilter(df => ({ ...df, preset: 'custom' }))
    } else {
      const range = computeRange(p)
      setDateFilter(df => ({ ...df, preset: p, from: range.from, to: range.to }))
    }
  }

  async function doSync(dryRun) {
    if (!hasRange) {
      setError('Date range is required. Choose a date range before syncing.')
      return
    }
    setError(null)
    setSyncing(true)
    setSyncResult(null)
    try {
      const res = await syncApi(dryRun, {
        jira, azure, mapping,
        date_filter: { field, from_date: from || null, to_date: to || null },
      })
      if (res.status === 'error') {
        setError(res.error || 'Unknown error')
      } else {
        setSyncResult({ res, dryRun })
      }
    } catch (e) {
      setError(`Request failed: ${e.message}`)
    }
    setSyncing(false)
  }

  function confirmSync() {
    if (!window.confirm('Run LIVE sync? This will create/update work items in Azure DevOps.')) return
    doSync(false)
  }

  return (
    <div>
      {/* ── Date filter ─────────────────────────────────────────── */}
      <div className="date-filter">
        <div className="df-label">
          Date Range <span className="df-required">Required</span>
        </div>
        <div className="df-presets">
          {PRESETS.map(p => (
            <button key={p.id}
              className={`df-btn${preset === p.id ? ' active' : ''}`}
              onClick={() => setPreset(p.id)}>
              {p.label}
            </button>
          ))}
        </div>

        {preset === 'custom' && (
          <div className="df-custom-row">
            <div className="df-field">
              <label className="df-field-label">From</label>
              <input type="date" value={from || ''}
                onChange={e => setDateFilter(df => ({ ...df, from: e.target.value || null }))} />
            </div>
            <div className="df-field">
              <label className="df-field-label">To</label>
              <input type="date" value={to || ''}
                onChange={e => setDateFilter(df => ({ ...df, to: e.target.value || null }))} />
            </div>
            <div className="df-field">
              <label className="df-field-label">Filter by</label>
              <select value={field}
                onChange={e => setDateFilter(df => ({ ...df, field: e.target.value }))}>
                <option value="updated">Updated date</option>
                <option value="created">Created date</option>
                <option value="either">Created or Updated</option>
              </select>
            </div>
          </div>
        )}

        {hasRange ? (
          <div className="df-summary">
            <DateSummary from={from} to={to} field={field} />
          </div>
        ) : (
          <div className="df-summary df-summary--error">
            ⚠️ Date range is required. Enter a From and/or To date to proceed.
          </div>
        )}
      </div>

      {/* ── Actions ─────────────────────────────────────────────── */}
      <div className="btn-row sync-actions">
        <button className="btn btn-secondary" onClick={() => doSync(true)} disabled={syncing}>
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
            <circle cx="8" cy="8" r="6"/><path d="M8 5v3l2 1.5"/>
          </svg>
          Preview
        </button>
        <button className="btn btn-primary" onClick={confirmSync} disabled={syncing}>
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M13.5 7A6 6 0 0 0 3 4.5M2.5 2.5v3h3"/>
            <path d="M2.5 9a6 6 0 0 0 10.5 2.5M13.5 13.5v-3h-3"/>
          </svg>
          Run Sync
        </button>
      </div>

      {syncing && (
        <div className="spinner active">
          <div className="spin-ring" />
          Syncing — this may take a minute…
        </div>
      )}

      {error && <div className="result error">{error}</div>}

      {syncResult && (
        <div className="srp-host">
          <SyncResultPanel res={syncResult.res} dryRun={syncResult.dryRun} />
        </div>
      )}
    </div>
  )
}
