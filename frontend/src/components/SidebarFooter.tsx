import type { Copy, Language } from '../i18n/types'

export function SidebarFooter({
  copy,
  language,
  onToggleLanguage,
}: {
  copy: Copy
  language: Language
  onToggleLanguage: () => void
}) {
  const legalDocumentsUrl = 'https://github.com/diamond447/BikeMapy/blob/main/docs'
  return (
    <footer className="app-footer" aria-label={copy.footer}>
      <div className="app-footer-language">
        <button
          type="button"
          className="language-switcher"
          aria-label={copy.changeLanguage}
          aria-pressed={language === 'cs'}
          onClick={onToggleLanguage}
        >
          {language === 'en' ? 'EN / CZ' : 'CZ / EN'}
        </button>
      </div>
      <span>{copy.independentProject}</span>
      <nav aria-label={copy.footer}>
        <a href={`${legalDocumentsUrl}/terms.md`} target="_blank" rel="noreferrer">
          {copy.terms}
        </a>
        <a href={`${legalDocumentsUrl}/privacy.md`} target="_blank" rel="noreferrer">
          {copy.privacy}
        </a>
        <a href={`${legalDocumentsUrl}/removal-policy.md`} target="_blank" rel="noreferrer">
          {copy.removalPolicy}
        </a>
      </nav>
      <span className="app-footer-attribution">
        {copy.mapAttribution}:{' '}
        <a href="https://openfreemap.org/" target="_blank" rel="noreferrer">
          OpenFreeMap
        </a>{' '}
        <a href="https://www.openmaptiles.org/" target="_blank" rel="noreferrer">
          © OpenMapTiles
        </a>{' '}
        Data from{' '}
        <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">
          OpenStreetMap
        </a>
      </span>
    </footer>
  )
}
