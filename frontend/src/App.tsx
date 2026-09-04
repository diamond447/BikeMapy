function App() {
  return (
    <main className="app-shell">
      <header className="topbar">
        <a className="wordmark" href="/" aria-label="BikeMapy home">
          <span className="wordmark-mark" aria-hidden="true">
            ↗
          </span>
          <span>BikeMapy</span>
        </a>
        <nav aria-label="Primary navigation">
          <a href="#about">About</a>
          <button type="button" className="language-switcher" aria-label="Change language">
            EN / CZ
          </button>
        </nav>
      </header>

      <section className="map-stage" aria-label="Route map">
        <div className="map-grid" aria-hidden="true" />
        <svg
          className="route-lines"
          viewBox="0 0 1000 660"
          role="img"
          aria-label="Illustration of routes across a map"
        >
          <path
            className="route route-muted"
            d="M32 564 C 170 510, 135 344, 292 370 S 515 515, 638 400 S 770 155, 980 88"
          />
          <path
            className="route route-muted route-secondary"
            d="M5 210 C 164 310, 282 95, 438 208 S 677 445, 1002 350"
          />
          <path
            className="route route-selected"
            d="M115 605 C 126 457, 280 505, 342 385 S 445 151, 621 190 S 824 311, 913 42"
          />
          <circle className="route-pin" cx="115" cy="605" r="8" />
          <circle className="route-pin" cx="913" cy="42" r="8" />
        </svg>
        <div className="map-label label-brno">Brno</div>
        <div className="map-label label-mikulov">Mikulov</div>
        <div className="map-controls" aria-label="Map controls">
          <button type="button" aria-label="Zoom in">
            +
          </button>
          <button type="button" aria-label="Zoom out">
            −
          </button>
          <button type="button" aria-label="Locate me">
            ◎
          </button>
        </div>
        <div className="map-caption">Illustrative map · route data coming soon</div>
      </section>

      <aside className="route-panel" aria-label="Route search">
        <p className="eyebrow">A map with a memory</p>
        <h1>
          Find the ride
          <br />
          worth repeating.
        </h1>
        <p className="intro">
          A calm catalogue of cycling routes shared by people who know the roads.
        </p>
        <label className="search-field">
          <span className="sr-only">Search routes</span>
          <span aria-hidden="true">⌕</span>
          <input type="search" placeholder="Search places, authors, routes" />
        </label>
        <div className="panel-footer">
          <span>0 routes indexed</span>
          <span className="status-dot" /> Building the archive
        </div>
      </aside>
    </main>
  )
}

export default App
