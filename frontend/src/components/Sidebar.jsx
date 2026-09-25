import jmanLogo from '../assets/jman_logo.svg'

function SidebarBackground() {
  return (
    <svg
      style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', pointerEvents: 'none' }}
      viewBox="0 0 256 900"
      preserveAspectRatio="xMidYMid slice"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
    >
      <defs>
        <style>{`
          @keyframes sfloat1 { 0%,100%{transform:translateY(0px)} 50%{transform:translateY(-14px)} }
          @keyframes sfloat2 { 0%,100%{transform:translateY(0px)} 50%{transform:translateY(-10px)} }
          @keyframes sfloat3 { 0%,100%{transform:translateY(0px)} 50%{transform:translateY(-18px)} }
          @keyframes sdash   { to { stroke-dashoffset: -36; } }
          .sf1 { animation: sfloat1 7s ease-in-out infinite; }
          .sf2 { animation: sfloat2 9s ease-in-out infinite 1.2s; }
          .sf3 { animation: sfloat3 6s ease-in-out infinite 2.4s; }
          .sline { stroke-dasharray:5 4; animation: sdash 3.5s linear infinite; }
        `}</style>
      </defs>

      {/* TOP LEFT — Kanban board: 3 columns with stacked task cards */}
      <g transform="translate(10, 52)">
        <g className="sf2" opacity="0.10">
          <rect x="0"  y="0" width="21" height="44" rx="3" fill="none" stroke="#ff6196" strokeWidth="1.2"/>
          <rect x="25" y="0" width="21" height="44" rx="3" fill="none" stroke="#ff6196" strokeWidth="1.2"/>
          <rect x="50" y="0" width="21" height="44" rx="3" fill="none" stroke="#ff6196" strokeWidth="1.2"/>
          <rect x="3"  y="4"  width="15" height="6" rx="1.5" fill="#ff6196"/>
          <rect x="3"  y="13" width="15" height="6" rx="1.5" fill="#ff6196"/>
          <rect x="3"  y="22" width="15" height="6" rx="1.5" fill="#ff6196"/>
          <rect x="28" y="4"  width="15" height="6" rx="1.5" fill="#ff6196"/>
          <rect x="28" y="13" width="15" height="6" rx="1.5" fill="#ff6196"/>
          <rect x="53" y="4"  width="15" height="6" rx="1.5" fill="#ff6196"/>
        </g>
      </g>

      {/* TOP RIGHT — Sync / refresh arrows (Jira ↔ Azure) */}
      <g transform="translate(158, 32)">
        <g className="sf1" opacity="0.08">
          <path d="M4,13 C4,4 30,0 38,9" fill="none" stroke="#ff6196" strokeWidth="2" strokeLinecap="round"/>
          <polygon points="34,4 40,9 34,14" fill="#ff6196"/>
          <path d="M38,22 C38,31 12,35 4,26" fill="none" stroke="#ff6196" strokeWidth="2" strokeLinecap="round"/>
          <polygon points="8,30 2,25 8,20" fill="#ff6196"/>
        </g>
      </g>

      {/* MID LEFT — Jira ticket stack (4 rows with status dot + title + priority tag) */}
      <g transform="translate(8, 282)">
        <g className="sf3" opacity="0.09">
          <rect x="0" y="0"  width="74" height="11" rx="2" fill="none" stroke="#ff6196" strokeWidth="0.9"/>
          <circle cx="6.5" cy="5.5"  r="3" fill="#ff6196"/>
          <rect x="14" y="3"  width="36" height="2" rx="1" fill="#ff6196" opacity="0.85"/>
          <rect x="55" y="2.5" width="14" height="6" rx="1.5" fill="#ff6196" opacity="0.5"/>

          <rect x="0" y="15" width="74" height="11" rx="2" fill="none" stroke="#ff6196" strokeWidth="0.9"/>
          <circle cx="6.5" cy="20.5" r="3" fill="#ff6196"/>
          <rect x="14" y="18" width="28" height="2" rx="1" fill="#ff6196" opacity="0.85"/>
          <rect x="55" y="17.5" width="14" height="6" rx="1.5" fill="#ff6196" opacity="0.5"/>

          <rect x="0" y="30" width="74" height="11" rx="2" fill="none" stroke="#ff6196" strokeWidth="0.9"/>
          <circle cx="6.5" cy="35.5" r="3" fill="none" stroke="#ff6196" strokeWidth="0.9"/>
          <rect x="14" y="33" width="32" height="2" rx="1" fill="#ff6196" opacity="0.85"/>
          <rect x="55" y="32.5" width="14" height="6" rx="1.5" fill="#ff6196" opacity="0.5"/>

          <rect x="0" y="45" width="74" height="11" rx="2" fill="none" stroke="#ff6196" strokeWidth="0.9"/>
          <circle cx="6.5" cy="50.5" r="3" fill="none" stroke="#ff6196" strokeWidth="0.9"/>
          <rect x="14" y="48" width="20" height="2" rx="1" fill="#ff6196" opacity="0.85"/>
          <rect x="55" y="47.5" width="14" height="6" rx="1.5" fill="#ff6196" opacity="0.5"/>
        </g>
      </g>

      {/* MID RIGHT — Assignee avatars (3 person icons) */}
      <g transform="translate(160, 268)">
        <g className="sf2" opacity="0.09">
          <circle cx="10" cy="8"  r="6.5" fill="none" stroke="#ff6196" strokeWidth="1.2"/>
          <path d="M1,26 Q10,19 19,26" fill="none" stroke="#ff6196" strokeWidth="1.2" strokeLinecap="round"/>

          <circle cx="38" cy="8"  r="6.5" fill="none" stroke="#ff6196" strokeWidth="1.2"/>
          <path d="M29,26 Q38,19 47,26" fill="none" stroke="#ff6196" strokeWidth="1.2" strokeLinecap="round"/>

          <circle cx="66" cy="8"  r="6.5" fill="none" stroke="#ff6196" strokeWidth="1.2"/>
          <path d="M57,26 Q66,19 75,26" fill="none" stroke="#ff6196" strokeWidth="1.2" strokeLinecap="round"/>
        </g>
      </g>

      {/* BOTTOM LEFT — Jira → Azure pipeline with arrow */}
      <g transform="translate(8, 570)">
        <g className="sf1" opacity="0.09">
          <rect x="0"  y="5" width="32" height="20" rx="4" fill="none" stroke="#ff6196" strokeWidth="1.2"/>
          <rect x="5"  y="10" width="22" height="2.5" rx="1" fill="#ff6196"/>
          <rect x="5"  y="15" width="14" height="2.5" rx="1" fill="#ff6196"/>
          <line x1="33" y1="15" x2="52" y2="15" stroke="#ff6196" strokeWidth="1.2" strokeDasharray="3 2"/>
          <polygon points="49,11 56,15 49,19" fill="#ff6196"/>
          <rect x="57" y="5" width="32" height="20" rx="4" fill="none" stroke="#ff6196" strokeWidth="1.2"/>
          <rect x="62" y="10" width="22" height="2.5" rx="1" fill="#ff6196"/>
          <rect x="62" y="15" width="14" height="2.5" rx="1" fill="#ff6196"/>
        </g>
      </g>

      {/* BOTTOM RIGHT — Checklist: 2 done (strikethrough), 1 pending */}
      <g transform="translate(150, 702)">
        <g className="sf3" opacity="0.08">
          <rect x="0" y="0"  width="11" height="11" rx="2" fill="#ff6196"/>
          <path d="M2,5.5 L4.5,8 L9,3" fill="none" stroke="#fff" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
          <rect x="15" y="4"  width="44" height="3" rx="1.5" fill="#ff6196" opacity="0.6"/>
          <line x1="15" y1="5.5" x2="59" y2="5.5" stroke="#ff6196" strokeWidth="0.9"/>

          <rect x="0" y="16" width="11" height="11" rx="2" fill="#ff6196"/>
          <path d="M2,21.5 L4.5,24 L9,19" fill="none" stroke="#fff" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
          <rect x="15" y="20" width="36" height="3" rx="1.5" fill="#ff6196" opacity="0.6"/>
          <line x1="15" y1="21.5" x2="51" y2="21.5" stroke="#ff6196" strokeWidth="0.9"/>

          <rect x="0" y="32" width="11" height="11" rx="2" fill="none" stroke="#ff6196" strokeWidth="1"/>
          <rect x="15" y="36" width="50" height="3" rx="1.5" fill="#ff6196"/>
        </g>
      </g>

      {/* Animated connector lines */}
      <g opacity="0.05">
        <line className="sline" x1="30"  y1="592" x2="120" y2="392" stroke="#ff6196" strokeWidth="1.2"/>
        <line className="sline" x1="185" y1="78"  x2="240" y2="272" stroke="#ff6196" strokeWidth="1.2"/>
        <line className="sline" x1="58"  y1="178" x2="200" y2="292" stroke="#ff6196" strokeWidth="1.2"/>
        <line className="sline" x1="152" y1="722" x2="220" y2="548" stroke="#ff6196" strokeWidth="1.2"/>
      </g>
    </svg>
  )
}

const ICONS = {
  connection: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M6 3H4a2 2 0 000 4h2M10 3h2a2 2 0 010 4h-2M5 5h6"/>
    </svg>
  ),
  mapping: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 4h7M12 4h2M12 4l-2-2m2 2l-2 2M14 12H7M4 12H2M4 12l2-2m-2 2l2 2"/>
    </svg>
  ),
  priority: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
      <path d="M3 4h10M3 8h7M3 12h4"/>
    </svg>
  ),
  assignee: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
      <circle cx="8" cy="5.5" r="2.5"/>
      <path d="M2.5 14.5c0-2.761 2.462-4 5.5-4s5.5 1.239 5.5 4"/>
    </svg>
  ),
  sync: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M13.5 7A6 6 0 0 0 3 4.5M2.5 2.5v3h3"/>
      <path d="M2.5 9a6 6 0 0 0 10.5 2.5M13.5 13.5v-3h-3"/>
    </svg>
  ),
}

const SETUP_LINKS = [
  { id: 'connection', label: 'Connection' },
  { id: 'mapping',    label: 'Status Mapping' },
  { id: 'priority',   label: 'Priority Mapping' },
  { id: 'assignee',   label: 'Assignee Overrides' },
]

export default function Sidebar({ activeSection, onNav }) {
  return (
    <aside className="sidebar">
      <SidebarBackground />

      <div className="sidebar-content">
        <div className="sidebar-logo">
          <div className="sidebar-logo-inner">
            <img src={jmanLogo} alt="JMAN" className="jman-logo-img" />
            <div className="sidebar-product-name">
              <span className="product-serif">Board</span>
              <span className="product-mono">Automater</span>
            </div>
          </div>
        </div>

        <nav className="sidebar-nav">
          <div className="nav-group-label">Setup</div>
          {SETUP_LINKS.map(({ id, label }) => (
            <button
              key={id}
              className={`nav-link${activeSection === id ? ' active' : ''}`}
              onClick={() => onNav(id)}
            >
              {ICONS[id]}
              {label}
            </button>
          ))}

          <div className="nav-divider" />
          <div className="nav-group-label">Actions</div>

          <button
            className={`nav-link${activeSection === 'sync' ? ' active' : ''}`}
            onClick={() => onNav('sync')}
          >
            {ICONS.sync}
            Run Sync
          </button>
        </nav>

      </div>
    </aside>
  )
}
