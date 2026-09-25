import { useState, useEffect } from 'react'
import Sidebar from './components/Sidebar'
import Topbar from './components/Topbar'
import Connection from './components/sections/Connection'
import StatusMapping from './components/sections/StatusMapping'
import PriorityMapping from './components/sections/PriorityMapping'
import AssigneeOverrides from './components/sections/AssigneeOverrides'
import RunSync from './components/sections/RunSync'
import { getDefaultConfig } from './utils/api'
import { computeRange } from './utils/dates'

const LS_KEY = 'board_automater_config_v1'

const DEFAULTS = {
  jira: {
    base_url:    '',
    email:       '',
    api_token:   '',
    project_key: '',
  },
  azure: {
    org:     '',
    project: '',
    team:    '',
    pat:     '',
  },
}

const SECTION_META = {
  connection: { step: 1, title: 'Connection',         sub: 'Jira and Azure DevOps credentials',                                      action: false },
  mapping:    { step: 2, title: 'Status Mapping',     sub: 'Jira status → Azure DevOps state · first match wins, top to bottom',     action: false },
  priority:   { step: 3, title: 'Priority Mapping',   sub: 'Jira priority → Azure numeric value and label',                          action: false },
  assignee:   { step: 4, title: 'Assignee Overrides', sub: 'Map Jira email → Azure email when they differ',                          action: false },
  sync:       { step: 5, title: 'Run Sync',            sub: null /* dynamic */,                                                       action: true  },
}

export default function App() {
  const [activeSection, setActiveSection] = useState('connection')

  const [jira,  setJira]  = useState({ ...DEFAULTS.jira })
  const [azure, setAzure] = useState({ ...DEFAULTS.azure })

  const [storyRules,        setStoryRules]        = useState([])
  const [taskRules,         setTaskRules]          = useState([])
  const [priorityRules,     setPriorityRules]      = useState([])
  const [assigneeOverrides, setAssigneeOverrides]  = useState([]) // [{ from, to }]

  const [dateFilter, setDateFilter] = useState(() => {
    const r = computeRange('this-week')
    return { preset: 'this-week', from: r.from, to: r.to, field: 'updated' }
  })

  useEffect(() => {
    async function init() {
      try {
        const cfg = await getDefaultConfig()
        setStoryRules(cfg.story_status_rules || [])
        setTaskRules(cfg.task_status_rules   || [])
        setPriorityRules(cfg.priority_rules  || [])
        const ao = cfg.assignee_overrides || {}
        setAssigneeOverrides(Object.entries(ao).map(([from, to]) => ({ from, to })))
      } catch {}

      const raw = localStorage.getItem(LS_KEY)
      if (!raw) return
      try {
        const saved = JSON.parse(raw)
        if (saved.jira)  setJira(j => ({ ...j, ...saved.jira }))
        if (saved.azure) setAzure(a => ({ ...a, ...saved.azure }))
        const m = saved.mapping || {}
        if (m.story_status_rules?.length) setStoryRules(m.story_status_rules)
        if (m.task_status_rules?.length)  setTaskRules(m.task_status_rules)
        if (m.priority_rules?.length)     setPriorityRules(m.priority_rules)
        if (m.assignee_overrides) {
          setAssigneeOverrides(
            Object.entries(m.assignee_overrides).map(([from, to]) => ({ from, to }))
          )
        }
      } catch {}
    }
    init()
  }, [])

  const mapping = {
    story_status_rules: storyRules,
    task_status_rules:  taskRules,
    priority_rules:     priorityRules,
    assignee_overrides: Object.fromEntries(
      assigneeOverrides.filter(e => e.from && e.to).map(({ from, to }) => [from, to])
    ),
  }

  function saveConfig() {
    localStorage.setItem(LS_KEY, JSON.stringify({ jira, azure, mapping }))
  }

  function resetConfig() {
    localStorage.removeItem(LS_KEY)
    setJira({ ...DEFAULTS.jira })
    setAzure({ ...DEFAULTS.azure })
  }

  // Dynamic subtitle for Run Sync panel
  const syncSub = (() => {
    const { from, to } = dateFilter
    if (!from && !to) return 'No date range selected — required before syncing'
    if (from && to)   return `${from} → ${to}`
    if (from)         return `From ${from}`
    return `Up to ${to}`
  })()

  const meta = SECTION_META[activeSection] || SECTION_META.connection
  const panelSub = activeSection === 'sync' ? syncSub : meta.sub

  return (
    <div className="shell">
      <Sidebar activeSection={activeSection} onNav={setActiveSection} />

      <div className="main-col">
        <Topbar />

        {/* ── Single-panel content area — one section at a time, scrolls internally ── */}
        <div className="content-pane">
          <div className="sp">
            {/* Panel header */}
            <div className="sp-hd">
              <div className={`accord-step${meta.action ? ' accord-step--action' : ''}`}>
                {meta.step}
              </div>
              <div className="accord-info">
                <div className="accord-title">{meta.title}</div>
                <div className="accord-sub">{panelSub}</div>
              </div>
            </div>

            {/* Panel body — scrolls independently */}
            <div className="sp-body">
              {activeSection === 'connection' && (
                <Connection
                  jira={jira} setJira={setJira}
                  azure={azure} setAzure={setAzure}
                  mapping={mapping}
                  onSave={saveConfig}
                  onReset={resetConfig}
                />
              )}
              {activeSection === 'mapping' && (
                <StatusMapping
                  storyRules={storyRules} setStoryRules={setStoryRules}
                  taskRules={taskRules}   setTaskRules={setTaskRules}
                />
              )}
              {activeSection === 'priority' && (
                <PriorityMapping rules={priorityRules} setRules={setPriorityRules} />
              )}
              {activeSection === 'assignee' && (
                <AssigneeOverrides overrides={assigneeOverrides} setOverrides={setAssigneeOverrides} />
              )}
              {activeSection === 'sync' && (
                <RunSync
                  jira={jira} azure={azure} mapping={mapping}
                  dateFilter={dateFilter} setDateFilter={setDateFilter}
                />
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
