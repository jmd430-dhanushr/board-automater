const FIELD_LABELS = { updated: 'last updated', created: 'created', either: 'created or updated' }

function ResultSection({ items, cls, label }) {
  if (!items.length) return null
  return (
    <div className={`srp-section srp-section--${cls}`}>
      <div className="srp-section-hd">
        <span>{label}</span>
        <span className="srp-section-count">{items.length}</span>
      </div>
      <div className="srp-items">
        {items.map((item, i) => (
          <div key={i} className={`srp-row srp-row--${cls}`}>
            <span className="srp-key">{item.jira_key || ''}</span>
            <span className="srp-title">
              {item.jira_title || ''}
              {item.error
                ? <span className="srp-err"> {item.error}</span>
                : cls === 'create' && item.result?.azure_id
                  ? <span className="srp-az"> · Azure #{item.result.azure_id}</span>
                  : null}
            </span>
            <span className={`srp-tag srp-tag--${cls}`}>{label}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

export default function SyncResultPanel({ res, dryRun }) {
  const s   = res.summary || {}
  const det = res.details || {}
  const created = det.created || []
  const updated = det.updated || []
  const skipped = det.skipped || []
  const failed  = det.failed  || []

  const total    = s.total_jira_tickets || (created.length + updated.length + skipped.length + failed.length)
  const timeStr  = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  const cLabel   = dryRun ? 'to create' : 'created'
  const uLabel   = dryRun ? 'to update' : 'updated'
  const badgeCls = dryRun ? 'srp-badge--preview' : 'srp-badge--live'

  const df = res.date_filter_applied
  let filterLine = null
  if (df) {
    const fl  = FIELD_LABELS[df.field] || df.field
    const rng = df.from_date && df.to_date ? `${df.from_date} → ${df.to_date}`
              : df.from_date               ? `from ${df.from_date}`
              :                              `up to ${df.to_date}`
    filterLine = <div className="srp-filter">Tickets <strong>{fl}</strong> · <strong>{rng}</strong></div>
  }

  const hasAny = created.length || updated.length || skipped.length || failed.length

  return (
    <div className="srp">
      <div className="srp-head">
        <div className="srp-head-left">
          <span className={`srp-badge ${badgeCls}`}>{dryRun ? 'Dry Run Preview' : 'Sync Completed'}</span>
          <span className="srp-meta">{total} ticket{total !== 1 ? 's' : ''} found in Jira</span>
        </div>
        <span className="srp-meta">{timeStr}</span>
      </div>

      {filterLine}

      <div className="srp-stats">
        <div className="srp-stat srp-stat--create">
          <div className="srp-stat-num">{created.length}</div>
          <div className="srp-stat-label">{cLabel}</div>
        </div>
        <div className="srp-stat srp-stat--update">
          <div className="srp-stat-num">{updated.length}</div>
          <div className="srp-stat-label">{uLabel}</div>
        </div>
        <div className="srp-stat srp-stat--skip">
          <div className="srp-stat-num">{skipped.length}</div>
          <div className="srp-stat-label">skipped</div>
        </div>
        <div className="srp-stat srp-stat--fail">
          <div className="srp-stat-num">{failed.length}</div>
          <div className="srp-stat-label">failed</div>
        </div>
      </div>

      {hasAny ? (
        <div className="srp-sections">
          <ResultSection items={created} cls="create" label={dryRun ? 'To Create' : 'Created'} />
          <ResultSection items={updated} cls="update" label={dryRun ? 'To Update' : 'Updated'} />
          <ResultSection items={skipped} cls="skip"   label="Skipped" />
          <ResultSection items={failed}  cls="fail"   label="Failed" />
        </div>
      ) : (
        <div className="srp-empty">No tickets matched the selected date range.</div>
      )}
    </div>
  )
}
