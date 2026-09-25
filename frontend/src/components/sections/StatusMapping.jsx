function RuleRow({ rule, onChange, onRemove }) {
  const naVal = rule.no_assignee === true ? 'true' : rule.no_assignee === false ? 'false' : ''

  return (
    <div className="rule-row">
      <div>
        <div className="rule-col-label">Keywords</div>
        <input type="text" className="field-input rule-match"
          value={rule.match.join(', ')}
          onChange={e => onChange({ ...rule, match: e.target.value.split(',').map(s => s.trim()).filter(Boolean) })}
          placeholder="in progress" />
      </div>
      <div className="rule-arrow">→</div>
      <div>
        <div className="rule-col-label">Azure states</div>
        <input type="text" className="field-input rule-targets"
          value={rule.target_states.join(', ')}
          onChange={e => onChange({ ...rule, target_states: e.target.value.split(',').map(s => s.trim()).filter(Boolean) })}
          placeholder="Doing, In Progress" />
      </div>
      <div>
        <div className="rule-col-label">Assignee filter</div>
        <select className="rule-select"
          value={naVal}
          onChange={e => {
            const v = e.target.value
            onChange({ ...rule, no_assignee: v === 'true' ? true : v === 'false' ? false : null })
          }}>
          <option value="">Any</option>
          <option value="true">No assignee</option>
          <option value="false">Has assignee</option>
        </select>
      </div>
      <button className="btn-del" onClick={onRemove} title="Remove">✕</button>
    </div>
  )
}

function RuleGroup({ label, rules, setRules }) {
  function add() {
    setRules(r => [...r, { match: [], target_states: [], no_assignee: null }])
  }
  function update(i, next) {
    setRules(r => r.map((x, idx) => idx === i ? next : x))
  }
  function remove(i) {
    setRules(r => r.filter((_, idx) => idx !== i))
  }

  return (
    <div className="rules-group">
      <div className="rules-group-label">{label}</div>
      <div className="rule-table-header">
        <span>Match keywords (comma-sep)</span>
        <span />
        <span>Azure target states (comma-sep)</span>
        <span>Assignee filter</span>
        <span />
      </div>
      {rules.map((rule, i) => (
        <RuleRow key={i} rule={rule} onChange={next => update(i, next)} onRemove={() => remove(i)} />
      ))}
      <button className="btn btn-ghost btn-sm" style={{ marginTop: 8 }} onClick={add}>
        + Add rule
      </button>
    </div>
  )
}

export default function StatusMapping({ storyRules, setStoryRules, taskRules, setTaskRules }) {
  return (
    <div>
      <RuleGroup label="Story Rules" rules={storyRules} setRules={setStoryRules} />
      <div style={{ marginTop: 24 }}>
        <RuleGroup label="Task Rules" rules={taskRules} setRules={setTaskRules} />
      </div>
    </div>
  )
}
