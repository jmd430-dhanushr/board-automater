import { useRef, useEffect } from 'react'

export default function Accordion({ id, step, title, sub, isOpen, onToggle, action, children }) {
  const ref = useRef(null)
  const firstRender = useRef(true)

  useEffect(() => {
    if (firstRender.current) { firstRender.current = false; return }
    if (isOpen && ref.current) {
      setTimeout(() => ref.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }), 30)
    }
  }, [isOpen])

  return (
    <div ref={ref} className={`accord${isOpen ? ' open' : ''}`} id={`section-${id}`}>
      <button className="accord-hd" onClick={onToggle}>
        <div className={`accord-step${action ? ' accord-step--action' : ''}`}>{step}</div>
        <div className="accord-info">
          <div className="accord-title">{title}</div>
          <div className="accord-sub">{sub}</div>
        </div>
        <svg className="accord-chevron" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M4 6l4 4 4-4"/>
        </svg>
      </button>
      <div className="accord-body">
        <div className="accord-inner">
          <div className="accord-content">
            {children}
          </div>
        </div>
      </div>
    </div>
  )
}
