function AssigneeRow({ entry, onChange, onRemove }) {
  return (
    <tr>
      <td>
        <input type="email" value={entry.from}
          onChange={e => onChange({ ...entry, from: e.target.value })}
          placeholder="jira@example.com" />
      </td>
      <td>
        <input type="email" value={entry.to}
          onChange={e => onChange({ ...entry, to: e.target.value })}
          placeholder="azure@company.com" />
      </td>
      <td>
        <button className="btn-del" onClick={onRemove}>✕</button>
      </td>
    </tr>
  )
}

export default function AssigneeOverrides({ overrides, setOverrides }) {
  function add() {
    setOverrides(o => [...o, { from: '', to: '' }])
  }
  function update(i, next) {
    setOverrides(o => o.map((x, idx) => idx === i ? next : x))
  }
  function remove(i) {
    setOverrides(o => o.filter((_, idx) => idx !== i))
  }

  return (
    <div>
      <table className="map-table">
        <thead>
          <tr><th>Jira Email</th><th>Azure Email</th><th /></tr>
        </thead>
        <tbody>
          {overrides.map((entry, i) => (
            <AssigneeRow key={i} entry={entry} onChange={next => update(i, next)} onRemove={() => remove(i)} />
          ))}
        </tbody>
      </table>
      <button className="btn btn-ghost btn-sm" style={{ marginTop: 8 }} onClick={add}>
        + Add override
      </button>
    </div>
  )
}
