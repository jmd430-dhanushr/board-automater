import { useState } from 'react'

const FIELD_LABELS = { updated: 'last updated', created: 'created', either: 'created or updated' }

// Action → how it is shown. Preview wording is "will …", live wording is past tense.
const ACTIONS = {
  create:    { cls: 'create', preview: 'Will create', live: 'Created' },
  update:    { cls: 'update', preview: 'Will update', live: 'Updated' },
  skip:      { cls: 'skip',   preview: 'Skip',        live: 'Skipped' },
  fail:      { cls: 'fail',   preview: 'Failed',      live: 'Failed' },
  unchanged: { cls: 'skip',   preview: 'No changes',  live: 'No changes' },
  untouched: { cls: 'skip',   preview: 'Left alone',  live: 'Left alone' },
}

export function initialRun(dryRun) {
  return { dryRun, stage: 'Starting…', total: null, tickets: [], summary: null, error: null, finished: false }
}

// Folds one streamed server event into the run state.
export function applyEvent(run, ev) {
  switch (ev.type) {
    case 'stage':
      return { ...run, stage: ev.message }
    case 'start':
      return {
        ...run, total: ev.total, azureItems: ev.azure_items, dateFilter: ev.date_filter,
        stage: ev.total ? `Processing ${ev.total} ticket(s)` : 'No tickets found in the date range',
        tickets: ev.tickets.map(t => ({ ...t, status: 'queued', steps: [] })),
      }
    case 'ticket_start':
    case 'step':
      return {
        ...run,
        tickets: run.tickets.map(t => t.index !== ev.index ? t : {
          ...t, status: 'processing',
          steps: ev.message ? [...t.steps, ev.message] : t.steps,
        }),
      }
    case 'ticket_done':
      return {
        ...run,
        tickets: run.tickets.map(t => t.index !== ev.index ? t : { ...t, status: 'done', result: ev }),
      }
    case 'done':
      return { ...run, summary: ev.summary, finished: true, stage: 'Finished' }
    case 'error':
      return { ...run, error: ev.error, finished: true, stage: 'Stopped' }
    default:
      return run
  }
}

function ActionTag({ action, dryRun }) {
  const a = ACTIONS[action] || ACTIONS.skip
  return <span className={`srp-tag srp-tag--${a.cls}`}>{dryRun ? a.preview : a.live}</span>
}

function ChangeTable({ changes, isCreate }) {
  if (!changes?.length) return null
  return (
    <table className="sp-changes">
      <thead>
        <tr><th>Field</th>{!isCreate && <th>Azure now</th>}<th>{isCreate ? 'Value' : 'From Jira'}</th></tr>
      </thead>
      <tbody>
        {changes.map((c, i) => (
          <tr key={i}>
            <td className="sp-changes-field">{c.field}</td>
            {!isCreate && <td className="sp-changes-from">{c.from}</td>}
            <td className="sp-changes-to">{c.to}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function TaskDetails({ tasks, dryRun }) {
  if (!tasks?.length) return null
  return (
    <div className="sp-tasks">
      <div className="sp-subhd">Child task</div>
      {tasks.map((t, i) => (
        <div key={i} className="sp-task">
          <div className="sp-task-hd">
            <ActionTag action={t.action} dryRun={dryRun} />
            {t.task_id
              ? <a href={t.url} target="_blank" rel="noreferrer">Task #{t.task_id}</a>
              : <span className="sp-muted">{t.action === 'create' ? 'New task' : ''}</span>}
            {t.note && <span className="sp-muted">{t.note}</span>}
          </div>
          {t.error && <div className="sp-error">{t.error}</div>}
          {(t.notes || []).map((n, j) => <div key={j} className="sp-note">{n}</div>)}
          <ChangeTable changes={t.changes} isCreate={t.action === 'create'} />
        </div>
      ))}
    </div>
  )
}

function TicketRow({ t, dryRun }) {
  const r = t.result
  const [open, setOpen] = useState(false)
  const expanded = open || t.status === 'processing'
  const cls = r ? (ACTIONS[r.action]?.cls || 'skip') : t.status

  return (
    <div className={`sp-ticket sp-ticket--${cls}`}>
      <button className="sp-ticket-hd" onClick={() => setOpen(o => !o)} disabled={!r && t.status !== 'processing'}>
        <span className="sp-ticket-icon">
          {t.status === 'queued'     && <span className="sp-dot" />}
          {t.status === 'processing' && <span className="spin-ring" />}
          {r && <span className={`sp-mark sp-mark--${cls}`} />}
        </span>
        <span className="srp-key">{t.key}</span>
        <span className="sp-ticket-title">{t.title}</span>
        <span className="sp-ticket-right">
          {t.status === 'queued' && <span className="sp-muted">Waiting</span>}
          {t.status === 'processing' && <span className="sp-muted">{t.steps[t.steps.length - 1] || 'Working…'}</span>}
          {r?.has_task_errors && <span className="srp-tag srp-tag--fail">Task error</span>}
          {r && <ActionTag action={r.action} dryRun={dryRun} />}
          {r && <span className={`sp-chevron${expanded ? ' open' : ''}`} />}
        </span>
      </button>

      {expanded && (
        <div className="sp-ticket-body">
          {t.status === 'processing' && (
            <ol className="sp-steps">{t.steps.map((s, i) => <li key={i}>{s}</li>)}</ol>
          )}
          {r && (
            <>
              <div className="sp-facts">
                {r.jira_status && <span>Jira status: <strong>{r.jira_status}</strong></span>}
                {r.azure_id && (
                  <span>Azure: <a href={r.azure_url} target="_blank" rel="noreferrer">#{r.azure_id}</a>
                    {r.match && <span className="sp-muted"> · matched by {r.match}</span>}</span>
                )}
                {r.action === 'create' && !r.azure_id && <span className="sp-muted">A new User Story will be created</span>}
              </div>
              {r.error  && <div className="sp-error">{r.error}</div>}
              {r.reason && <div className="sp-note">{r.reason}</div>}
              {(r.notes || []).map((n, i) => <div key={i} className="sp-note">{n}</div>)}
              {r.changes?.length > 0 && (
                <>
                  <div className="sp-subhd">
                    User Story {r.action === 'skip' ? '— differences (not applied)' : r.action === 'create' ? '— fields' : '— changes'}
                  </div>
                  <ChangeTable changes={r.changes} isCreate={r.action === 'create'} />
                </>
              )}
              <TaskDetails tasks={r.tasks} dryRun={dryRun} />
              {t.steps.length > 0 && (
                <details className="sp-log">
                  <summary>Steps ({t.steps.length})</summary>
                  <ol className="sp-steps">{t.steps.map((s, i) => <li key={i}>{s}</li>)}</ol>
                </details>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}

const FILTERS = [
  { id: 'all', label: 'All' }, { id: 'create', label: 'Create' }, { id: 'update', label: 'Update' },
  { id: 'skip', label: 'Skipped' }, { id: 'fail', label: 'Failed' },
]

export default function SyncProgress({ run }) {
  const [filter, setFilter] = useState('all')
  const { dryRun, tickets } = run
  const doneCount = tickets.filter(t => t.status === 'done').length
  const count = a => tickets.filter(t => t.result?.action === a).length
  const pct = run.total ? Math.round((doneCount / run.total) * 100) : (run.finished ? 100 : 0)
  const shown = filter === 'all' ? tickets : tickets.filter(t => t.result?.action === filter)

  const df = run.dateFilter
  const rng = df && (df.from_date && df.to_date ? `${df.from_date} → ${df.to_date}` : df.from_date ? `from ${df.from_date}` : `up to ${df.to_date}`)

  return (
    <div className="srp">
      <div className="srp-head">
        <div className="srp-head-left">
          <span className={`srp-badge ${dryRun ? 'srp-badge--preview' : 'srp-badge--live'}`}>
            {dryRun ? 'Preview — nothing is changed' : 'Live sync'}
          </span>
          <span className="srp-meta">
            {!run.finished && <span className="spin-ring sp-inline-spin" />}
            {run.stage}
          </span>
        </div>
        {run.total != null && <span className="srp-meta">{doneCount} / {run.total} tickets</span>}
      </div>

      <div className="sp-bar"><div className={`sp-bar-fill${run.error ? ' sp-bar-fill--error' : ''}`} style={{ width: `${pct}%` }} /></div>

      {df && (
        <div className="srp-filter">
          Tickets <strong>{FIELD_LABELS[df.field] || df.field}</strong> · <strong>{rng}</strong>
          {run.azureItems != null && <> · {run.azureItems} work item(s) on the Azure team board</>}
        </div>
      )}

      {run.error && <div className="result error sp-run-error">{run.error}</div>}

      {run.total != null && (
        <div className="srp-stats">
          {[['create', dryRun ? 'to create' : 'created'], ['update', dryRun ? 'to update' : 'updated'],
            ['skip', 'skipped'], ['fail', 'failed']].map(([a, label]) => (
            <div key={a} className={`srp-stat srp-stat--${a}`}>
              <div className="srp-stat-num">{count(a)}</div>
              <div className="srp-stat-label">{label}</div>
            </div>
          ))}
        </div>
      )}

      {tickets.length > 0 && (
        <>
          <div className="sp-filters">
            {FILTERS.map(f => (
              <button key={f.id} className={`df-btn${filter === f.id ? ' active' : ''}`} onClick={() => setFilter(f.id)}>
                {f.label}{f.id !== 'all' && ` (${count(f.id)})`}
              </button>
            ))}
          </div>
          <div className="sp-list">
            {shown.map(t => <TicketRow key={t.index} t={t} dryRun={dryRun} />)}
            {shown.length === 0 && <div className="srp-empty">No tickets in this group.</div>}
          </div>
        </>
      )}

      {run.finished && !run.error && run.total === 0 && (
        <div className="srp-empty">No Jira tickets matched the selected date range.</div>
      )}

      {run.finished && !run.error && run.total > 0 && (
        <div className="sp-footer">
          {dryRun
            ? 'Preview finished. Nothing was changed in Azure DevOps. Review the changes above, then click Run Sync to apply them.'
            : 'Sync finished. Click any ticket to see exactly what was changed.'}
        </div>
      )}
    </div>
  )
}
