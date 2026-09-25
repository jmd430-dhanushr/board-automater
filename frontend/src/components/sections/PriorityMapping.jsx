function PriorityRow({ rule, onChange, onRemove }) {
  return (
    <tr>
      <td>
        <input type="text" value={rule.jira_priority}
          onChange={e => onChange({ ...rule, jira_priority: e.target.value })}
          placeholder="high" />
      </td>
      <td>
        <input type="number" value={rule.numeric} min="1" max="10" style={{ width: 70 }}
          onChange={e => onChange({ ...rule, numeric: parseInt(e.target.value, 10) || 3 })}
          placeholder="2" />
      </td>
      <td>
        <input type="text" value={rule.custom_label}
          onChange={e => onChange({ ...rule, custom_label: e.target.value })}
          placeholder="2 - High" />
      </td>
      <td>
        <button className="btn-del" onClick={onRemove}>✕</button>
      </td>
    </tr>
  )
}

export default function PriorityMapping({ rules, setRules }) {
  function add() {
    setRules(r => [...r, { jira_priority: '', numeric: 3, custom_label: '' }])
  }
  function update(i, next) {
    setRules(r => r.map((x, idx) => idx === i ? next : x))
  }
  function remove(i) {
    setRules(r => r.filter((_, idx) => idx !== i))
  }

  return (
    <div>
      <table className="map-table">
        <thead>
          <tr>
            <th>Jira Priority</th><th>Azure Numeric</th><th>Custom Label</th><th />
          </tr>
        </thead>
        <tbody>
          {rules.map((rule, i) => (
            <PriorityRow key={i} rule={rule} onChange={next => update(i, next)} onRemove={() => remove(i)} />
          ))}
        </tbody>
      </table>
      <button className="btn btn-ghost btn-sm" style={{ marginTop: 8 }} onClick={add}>
        + Add row
      </button>
    </div>
  )
}
