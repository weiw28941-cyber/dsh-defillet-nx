export const colors = {
  bg: '#0d1218',
  panel: '#171d24',
  border: '#303a45',
  text: '#e8edf2',
  muted: '#9da9b5',
  selected: '#ff5a3c',
  mapped: '#9b5cff',
  overlap: '#ff4fd8',
  hover: '#ffd166',
  danger: '#ff806f'
};

export const styles = {
  root: {
    boxSizing: 'border-box',
    height: '100%',
    minHeight: 0,
    padding: 18,
    color: `var(--dsw-color-text-primary,${colors.text})`,
    background: `var(--dsw-color-bg-primary,${colors.bg})`,
    overflow: 'auto',
    fontFamily: 'Inter,system-ui,sans-serif'
  },
  frame: {
    width: 'min(1500px,100%)',
    margin: '0 auto',
    display: 'grid',
    gap: 12
  },
  header: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: 12,
    flexWrap: 'wrap'
  },
  heading: {
    margin: 0,
    fontSize: 23,
    fontWeight: 650
  },
  lead: {
    margin: '5px 0 0',
    color: colors.muted,
    fontSize: 12,
    lineHeight: 1.5
  },
  badge: {
    display: 'inline-flex',
    padding: '5px 9px',
    borderRadius: 999,
    color: '#74b9ff',
    background: 'rgba(42,125,225,.14)',
    fontSize: 11,
    fontWeight: 650
  },
  button: {
    border: `1px solid ${colors.border}`,
    borderRadius: 8,
    padding: '7px 10px',
    color: colors.text,
    background: '#1b2530',
    cursor: 'pointer',
    fontSize: 12
  },
  primary: {
    border: 'none',
    background: '#36b7e7',
    color: '#03111b',
    fontWeight: 700
  },
  danger: {
    borderColor: '#713f47',
    color: '#ff9b8c'
  },
  disabled: {
    opacity: .45,
    cursor: 'not-allowed'
  },
  toolbar: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    flexWrap: 'wrap'
  },
  status: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    padding: '9px 12px',
    border: `1px solid ${colors.border}`,
    borderRadius: 10,
    background: colors.panel,
    fontSize: 12
  },
  grid: {
    display: 'grid',
    gridTemplateColumns: 'minmax(0,1fr) minmax(0,1fr)',
    gap: 12
  },
  viewerCard: {
    minWidth: 0,
    border: `1px solid ${colors.border}`,
    borderRadius: 12,
    overflow: 'hidden',
    background: colors.panel
  },
  viewerHeader: {
    height: 42,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    padding: '0 12px',
    borderBottom: `1px solid ${colors.border}`,
    fontSize: 12
  },
  viewer: {
    position: 'relative',
    height: 440,
    background: '#09111e'
  },
  canvas: {
    display: 'block',
    width: '100%',
    height: '100%'
  },
  overlay: {
    position: 'absolute',
    left: 10,
    top: 10,
    zIndex: 2,
    padding: '5px 8px',
    borderRadius: 7,
    color: '#dbeafe',
    background: 'rgba(4,10,18,.76)',
    fontSize: 11,
    pointerEvents: 'none'
  },
  lower: {
    display: 'grid',
    gridTemplateColumns: '260px repeat(3,minmax(280px,1fr))',
    gap: 12
  },
  panel: {
    padding: 14,
    border: `1px solid ${colors.border}`,
    borderRadius: 12,
    background: colors.panel,
    minHeight: 180,
    overflow: 'auto'
  },
  title: {
    margin: '0 0 10px',
    fontSize: 13,
    fontWeight: 700
  },
  row: {
    display: 'grid',
    gridTemplateColumns: '1fr auto',
    gap: 8,
    padding: '8px 9px',
    marginBottom: 6,
    borderRadius: 8,
    background: 'rgba(255,90,60,.09)',
    fontSize: 11
  },
  muted: {
    color: colors.muted
  },
  empty: {
    padding: 18,
    border: `1px dashed ${colors.border}`,
    borderRadius: 9,
    color: colors.muted,
    textAlign: 'center',
    fontSize: 12,
    lineHeight: 1.55
  },
  input: {
    boxSizing: 'border-box',
    width: '100%',
    padding: 9,
    border: `1px solid ${colors.border}`,
    borderRadius: 8,
    color: colors.text,
    background: '#101820',
    resize: 'vertical'
  },
  log: {
    margin: 0,
    maxHeight: 235,
    overflow: 'auto',
    whiteSpace: 'pre-wrap',
    fontSize: 11,
    lineHeight: 1.55,
    color: '#bdc9d6'
  },
  rev: {
    padding: '7px 8px',
    borderBottom: `1px solid ${colors.border}`,
    fontSize: 11
  },
  error: {
    color: colors.danger
  },
  file: {
    display: 'none'
  },
  unavailable: {
    minHeight: 420,
    display: 'grid',
    placeItems: 'center',
    padding: 32,
    textAlign: 'center'
  }
};
