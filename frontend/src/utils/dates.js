export function fmtDate(d) {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

export function toMonday(d) {
  const day = d.getDay() || 7
  const mon = new Date(d)
  mon.setDate(d.getDate() - day + 1)
  return mon
}

export function computeRange(preset) {
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  switch (preset) {
    case 'today':
      return { from: fmtDate(today), to: fmtDate(today) }
    case 'this-week':
      return { from: fmtDate(toMonday(today)), to: fmtDate(today) }
    case 'last-week': {
      const mon = toMonday(today)
      const lastMon = new Date(mon); lastMon.setDate(mon.getDate() - 7)
      const lastSun = new Date(lastMon); lastSun.setDate(lastMon.getDate() + 6)
      return { from: fmtDate(lastMon), to: fmtDate(lastSun) }
    }
    case '7d': {
      const d = new Date(today); d.setDate(today.getDate() - 6)
      return { from: fmtDate(d), to: fmtDate(today) }
    }
    case '30d': {
      const d = new Date(today); d.setDate(today.getDate() - 29)
      return { from: fmtDate(d), to: fmtDate(today) }
    }
    default:
      return { from: null, to: null }
  }
}
