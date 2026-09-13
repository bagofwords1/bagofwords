<script type="text/babel">
setTheme('atelier', {
  accent: '#b6533d',
  accent2: '#315b52',
  chart: ['#b6533d', '#315b52', '#c78d54', '#6b7180', '#8d6b5d', '#a8a28d', '#486b73', '#d2b29a'],
  radius: 'crisp',
  shadow: 'soft'
});

function App() {
  const data = useArtifactData();
  const theme = useTheme();
  const params = useParams();
  const genreOptions = useParamOptions('genre');

  const [selectedAlbumId, setSelectedAlbumId] = useState(null);
  const [sortBy, setSortBy] = useState('album');
  const [sortDirection, setSortDirection] = useState('asc');

  const albumViz = vizById('FIXTURE_VIZ_0');
  const trackViz = vizById('FIXTURE_VIZ_1');

  const albumRows = albumViz?.rows || [];
  const trackRows = trackViz?.rows || [];

  const sortedAlbums = useMemo(() => {
    const rows = [...albumRows];
    rows.sort((a, b) => {
      let left = a?.[sortBy];
      let right = b?.[sortBy];

      if (typeof left === 'string' || typeof right === 'string') {
        left = String(left ?? '').toLocaleLowerCase();
        right = String(right ?? '').toLocaleLowerCase();
      } else {
        left = left ?? 0;
        right = right ?? 0;
      }

      if (left < right) return sortDirection === 'asc' ? -1 : 1;
      if (left > right) return sortDirection === 'asc' ? 1 : -1;
      return 0;
    });
    return rows;
  }, [albumRows, sortBy, sortDirection]);

  const selectedAlbum = useMemo(() => {
    return albumRows.find(row => String(row?.album_id) === String(selectedAlbumId)) || null;
  }, [albumRows, selectedAlbumId]);

  const selectedTracks = useMemo(() => {
    if (!selectedAlbum) return [];
    return trackRows.filter(row => String(row?.album_id) === String(selectedAlbum.album_id));
  }, [trackRows, selectedAlbum]);

  useEffect(() => {
    if (selectedAlbumId === null && sortedAlbums.length > 0) {
      setSelectedAlbumId(sortedAlbums[0].album_id);
    }
  }, [selectedAlbumId, sortedAlbums]);

  useEffect(() => {
    if (
      selectedAlbumId !== null &&
      albumRows.length > 0 &&
      !albumRows.some(row => String(row?.album_id) === String(selectedAlbumId))
    ) {
      setSelectedAlbumId(null);
    }
  }, [albumRows, selectedAlbumId]);

  const updateNumberParam = (name, rawValue) => {
    const trimmed = String(rawValue ?? '').trim();
    params.setParam(name, trimmed === '' ? null : Number(trimmed));
  };

  const resetCatalog = () => {
    params.setParams({
      genre: null,
      search: '',
      max_price: 2,
      min_minutes: 0
    });
    setSelectedAlbumId(null);
    setSortBy('album');
    setSortDirection('asc');
  };

  const toggleSort = (field) => {
    if (sortBy === field) {
      setSortDirection(current => current === 'asc' ? 'desc' : 'asc');
    } else {
      setSortBy(field);
      setSortDirection('asc');
    }
  };

  const selectedGenre = params.values?.genre ?? '';
  const searchValue = params.values?.search ?? '';
  const maxPrice = params.values?.max_price ?? '';
  const minMinutes = params.values?.min_minutes ?? '';

  if (!data) {
    return (
      <div className="listening-room loading-screen">
        <LoadingSpinner size={30} />
        <span>Opening the catalog…</span>
      </div>
    );
  }

  return (
    <div className="listening-room">
      <style>{`
        .listening-room {
          --paper: #f5f0e8;
          --paper-deep: #ebe2d5;
          --ink: #282522;
          --muted: #766f67;
          --line: #d8cfc2;
          --rust: #b6533d;
          --rust-soft: #ead1c8;
          min-height: 100vh;
          background: var(--paper);
          color: var(--ink);
          font-family: var(--bow-font-body, Inter, sans-serif);
          padding: 30px clamp(18px, 4vw, 54px) 48px;
        }

        .listening-room * { box-sizing: border-box; }
        .lr-shell { max-width: 1400px; margin: 0 auto; }
        .lr-kicker {
          color: var(--rust);
          font-size: 11px;
          letter-spacing: .18em;
          text-transform: uppercase;
          font-weight: 700;
          margin-bottom: 13px;
        }
        .lr-title {
          font-family: var(--bow-font-display, Georgia, serif);
          font-size: clamp(42px, 7vw, 76px);
          font-weight: 500;
          line-height: .94;
          letter-spacing: -.045em;
          margin: 0;
        }
        .lr-intro {
          max-width: 620px;
          color: var(--muted);
          font-size: 15px;
          line-height: 1.6;
          margin: 18px 0 0;
        }
        .lr-rule { height: 1px; background: var(--line); margin: 28px 0 22px; }
        .lr-controls {
          display: grid;
          grid-template-columns: minmax(220px, 1.7fr) minmax(130px, .8fr) minmax(120px, .65fr) minmax(130px, .7fr) auto;
          gap: 10px;
          align-items: end;
        }
        .lr-field { min-width: 0; }
        .lr-label {
          display: block;
          color: var(--muted);
          font-size: 10px;
          font-weight: 700;
          letter-spacing: .12em;
          text-transform: uppercase;
          margin: 0 0 7px 2px;
        }
        .lr-input, .lr-select {
          width: 100%;
          height: 42px;
          border: 1px solid var(--line);
          border-radius: 5px;
          background: rgba(255,255,255,.42);
          color: var(--ink);
          padding: 0 12px;
          font: inherit;
          outline: none;
        }
        .lr-input:focus, .lr-select:focus {
          border-color: var(--rust);
          box-shadow: 0 0 0 3px rgba(182,83,61,.12);
        }
        .lr-button {
          height: 42px;
          border: 1px solid var(--ink);
          background: var(--ink);
          color: var(--paper);
          border-radius: 5px;
          padding: 0 16px;
          font: inherit;
          font-size: 13px;
          cursor: pointer;
          white-space: nowrap;
        }
        .lr-button:hover { background: var(--rust); border-color: var(--rust); }
        .lr-button.secondary {
          background: transparent;
          color: var(--ink);
          border-color: var(--line);
        }
        .lr-button.secondary:hover { background: var(--paper-deep); border-color: var(--ink); }
        .lr-status {
          min-height: 23px;
          display: flex;
          align-items: center;
          gap: 10px;
          color: var(--muted);
          font-size: 12px;
          margin-top: 11px;
        }
        .lr-error {
          color: #a73e35;
          background: #f4dcd6;
          border: 1px solid #dfb4a9;
          padding: 9px 11px;
          border-radius: 5px;
        }
        .lr-layout {
          display: grid;
          grid-template-columns: minmax(0, 1.1fr) minmax(320px, .9fr);
          gap: 22px;
          align-items: start;
          margin-top: 28px;
        }
        .lr-panel {
          border-top: 2px solid var(--ink);
          min-width: 0;
        }
        .lr-panel-head {
          display: flex;
          justify-content: space-between;
          align-items: baseline;
          gap: 14px;
          padding: 13px 0 12px;
          border-bottom: 1px solid var(--line);
        }
        .lr-panel-title {
          font-family: var(--bow-font-display, Georgia, serif);
          font-size: 25px;
          font-weight: 500;
          margin: 0;
        }
        .lr-count {
          color: var(--muted);
          font-size: 12px;
          white-space: nowrap;
        }
        .lr-sort {
          display: flex;
          gap: 7px;
          align-items: center;
          margin: 10px 0 2px;
          color: var(--muted);
          font-size: 11px;
        }
        .lr-sort button {
          border: 0;
          background: transparent;
          color: var(--muted);
          padding: 2px 0;
          font: inherit;
          cursor: pointer;
        }
        .lr-sort button.active { color: var(--rust); font-weight: 700; }
        .album-list { display: grid; }
        .album-row {
          display: grid;
          grid-template-columns: 46px minmax(0, 1fr) auto;
          gap: 13px;
          align-items: center;
          min-height: 82px;
          border-bottom: 1px solid var(--line);
          cursor: pointer;
          transition: background .16s ease, padding .16s ease;
        }
        .album-row:hover { background: rgba(255,255,255,.34); padding-left: 7px; }
        .album-row.selected {
          background: var(--rust-soft);
          padding-left: 10px;
          padding-right: 10px;
          margin-left: -10px;
          margin-right: -10px;
        }
        .album-mark {
          width: 42px;
          height: 42px;
          border-radius: 2px;
          background: var(--ink);
          color: var(--paper);
          display: grid;
          place-items: center;
          font-family: var(--bow-font-display, Georgia, serif);
          font-size: 19px;
        }
        .album-name {
          font-family: var(--bow-font-display, Georgia, serif);
          font-size: 20px;
          line-height: 1.05;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }
        .album-meta {
          color: var(--muted);
          font-size: 12px;
          margin-top: 6px;
        }
        .album-price {
          text-align: right;
          font-family: var(--bow-font-numeric, monospace);
          font-size: 14px;
        }
        .album-price small {
          display: block;
          color: var(--muted);
          font-family: var(--bow-font-body, sans-serif);
          font-size: 10px;
          margin-top: 4px;
        }
        .detail-panel {
          background: rgba(255,255,255,.34);
          border: 1px solid var(--line);
          padding: clamp(18px, 3vw, 30px);
          position: sticky;
          top: 18px;
        }
        .detail-mark {
          width: 94px;
          height: 94px;
          background: var(--ink);
          color: var(--paper);
          display: grid;
          place-items: center;
          font-family: var(--bow-font-display, Georgia, serif);
          font-size: 41px;
          margin-bottom: 23px;
        }
        .detail-genre {
          color: var(--rust);
          font-size: 10px;
          font-weight: 700;
          letter-spacing: .16em;
          text-transform: uppercase;
        }
        .detail-title {
          font-family: var(--bow-font-display, Georgia, serif);
          font-size: clamp(32px, 4vw, 48px);
          font-weight: 500;
          letter-spacing: -.04em;
          line-height: .95;
          margin: 8px 0 6px;
        }
        .detail-artist { color: var(--muted); font-size: 15px; }
        .detail-facts {
          display: grid;
          grid-template-columns: repeat(3, 1fr);
          border-top: 1px solid var(--line);
          border-bottom: 1px solid var(--line);
          margin: 23px 0 18px;
        }
        .detail-fact { padding: 12px 8px 12px 0; }
        .detail-fact + .detail-fact { border-left: 1px solid var(--line); padding-left: 12px; }
        .detail-fact-label {
          color: var(--muted);
          display: block;
          font-size: 10px;
          text-transform: uppercase;
          letter-spacing: .1em;
          margin-bottom: 5px;
        }
        .detail-fact-value { font-size: 15px; }
        .track-heading {
          display: flex;
          justify-content: space-between;
          align-items: baseline;
          margin: 0 0 6px;
        }
        .track-heading h3 {
          font-family: var(--bow-font-display, Georgia, serif);
          font-size: 22px;
          font-weight: 500;
          margin: 0;
        }
        .track-list { border-top: 1px solid var(--line); }
        .track-row {
          display: grid;
          grid-template-columns: 25px minmax(0, 1fr) auto;
          gap: 9px;
          align-items: center;
          min-height: 42px;
          border-bottom: 1px solid var(--line);
          font-size: 13px;
        }
        .track-number { color: var(--muted); font-family: monospace; font-size: 11px; }
        .track-time { color: var(--muted); font-family: monospace; font-size: 11px; }
        .empty {
          color: var(--muted);
          border: 1px dashed var(--line);
          padding: 30px 18px;
          text-align: center;
          font-size: 13px;
          line-height: 1.5;
        }
        .lr-note {
          color: var(--muted);
          font-size: 11px;
          line-height: 1.45;
          margin-top: 18px;
        }
        .loading-screen {
          display: flex;
          min-height: 70vh;
          align-items: center;
          justify-content: center;
          gap: 12px;
          color: var(--muted);
        }
        @media (max-width: 760px) {
          .listening-room { padding: 24px 16px 38px; }
          .lr-controls { grid-template-columns: 1fr 1fr; }
          .lr-controls .search-field { grid-column: 1 / -1; }
          .lr-controls .reset-button { grid-column: 1 / -1; }
          .lr-layout { grid-template-columns: 1fr; }
          .detail-panel { position: static; }
        }
        @media (max-width: 420px) {
          .lr-controls { grid-template-columns: 1fr; }
          .lr-controls .search-field, .lr-controls .reset-button { grid-column: auto; }
          .lr-title { font-size: 48px; }
          .album-row { grid-template-columns: 38px minmax(0, 1fr) auto; gap: 9px; }
          .album-mark { width: 36px; height: 36px; font-size: 16px; }
          .album-name { font-size: 18px; }
          .album-price { font-size: 12px; }
        }
      `}</style>

      <div className="lr-shell">
        <header>
          <div className="lr-kicker">Listening Room · catalog study</div>
          <h1 className="lr-title">A room for records.</h1>
          <p className="lr-intro">
            Search the full catalog by sound, artist, and duration. Select an album
            to open its track list and spend a little longer with the details.
          </p>
        </header>

        <div className="lr-rule" />

        <section aria-label="Catalog controls">
          <div className="lr-controls">
            <div className="lr-field search-field">
              <label className="lr-label" htmlFor="catalog-search">Search catalog</label>
              <input
                id="catalog-search"
                className="lr-input"
                value={searchValue}
                placeholder="Album, artist, or track"
                onChange={event => params.setParam('search', event.target.value)}
              />
            </div>

            <div className="lr-field">
              <label className="lr-label" htmlFor="catalog-genre">Genre</label>
              <select
                id="catalog-genre"
                className="lr-select"
                value={selectedGenre}
                onChange={event => params.setParam('genre', event.target.value || null)}
              >
                <option value="">All genres</option>
                {(genreOptions || []).map(option => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            </div>

            <div className="lr-field">
              <label className="lr-label" htmlFor="catalog-price">Max price</label>
              <input
                id="catalog-price"
                className="lr-input"
                type="number"
                min="0"
                step="0.01"
                value={maxPrice}
                onChange={event => updateNumberParam('max_price', event.target.value)}
              />
            </div>

            <div className="lr-field">
              <label className="lr-label" htmlFor="catalog-minutes">Min minutes</label>
              <input
                id="catalog-minutes"
                className="lr-input"
                type="number"
                min="0"
                step="1"
                value={minMinutes}
                onChange={event => updateNumberParam('min_minutes', event.target.value)}
              />
            </div>

            <button className="lr-button secondary reset-button" type="button" onClick={resetCatalog}>
              Clear / reset
            </button>
          </div>

          <div className="lr-status" aria-live="polite">
            {params.loading && <><LoadingSpinner size={16} /> Updating the catalog…</>}
            {!params.loading && !params.error && `${albumRows.length} album${albumRows.length === 1 ? '' : 's'} found`}
            {params.error && <span className="lr-error">{String(params.error)}</span>}
          </div>
        </section>

        <main className="lr-layout">
          <section className="lr-panel" aria-labelledby="albums-heading">
            <div className="lr-panel-head">
              <h2 id="albums-heading" className="lr-panel-title">Albums</h2>
              <span className="lr-count">source catalog</span>
            </div>

            <div className="lr-sort" aria-label="Sort albums">
              <span>Sort:</span>
              <button
                type="button"
                className={sortBy === 'album' ? 'active' : ''}
                onClick={() => toggleSort('album')}
              >
                title {sortBy === 'album' && (sortDirection === 'asc' ? '↑' : '↓')}
              </button>
              <button
                type="button"
                className={sortBy === 'year' ? 'active' : ''}
                onClick={() => toggleSort('year')}
              >
                year {sortBy === 'year' && (sortDirection === 'asc' ? '↑' : '↓')}
              </button>
              <button
                type="button"
                className={sortBy === 'minutes' ? 'active' : ''}
                onClick={() => toggleSort('minutes')}
              >
                duration {sortBy === 'minutes' && (sortDirection === 'asc' ? '↑' : '↓')}
              </button>
            </div>

            {albumRows.length === 0 ? (
              <div className="empty" style={{ marginTop: 12 }}>
                No albums match these catalog controls.<br />
                Try clearing the search or widening the filters.
              </div>
            ) : (
              <div className="album-list" data-bow-viz="FIXTURE_VIZ_0">
                {sortedAlbums.map(row => {
                  const isSelected = String(row.album_id) === String(selectedAlbumId);
                  const initial = String(row.album ?? '?').trim().charAt(0).toUpperCase();

                  return (
                    <article
                      key={row.album_id}
                      className={`album-row ${isSelected ? 'selected' : ''}`}
                      onClick={() => setSelectedAlbumId(row.album_id)}
                      onKeyDown={event => {
                        if (event.key === 'Enter' || event.key === ' ') {
                          event.preventDefault();
                          setSelectedAlbumId(row.album_id);
                        }
                      }}
                      tabIndex={0}
                      aria-selected={isSelected}
                      data-bow-viz="FIXTURE_VIZ_0"
                    >
                      <div className="album-mark" aria-hidden="true">{initial}</div>
                      <div>
                        <div className="album-name">{row.album}</div>
                        <div className="album-meta">
                          {row.artist} · {row.genre} · {row.year} · {row.tracks} tracks · {Number(row.minutes ?? 0).toFixed(1)} min
                        </div>
                      </div>
                      <div className="album-price">
                        {fmt(row.price, { currency: true })}
                        <small>album</small>
                      </div>
                    </article>
                  );
                })}
              </div>
            )}
          </section>

          <aside className="detail-panel" aria-labelledby="detail-heading">
            {!selectedAlbum ? (
              <div className="empty">
                This album is no longer in the current result set.<br />
                Choose another album or clear the catalog controls.
              </div>
            ) : (
              <div data-bow-viz="FIXTURE_VIZ_0">
                <div className="detail-mark" aria-hidden="true">
                  {String(selectedAlbum.album ?? '?').trim().charAt(0).toUpperCase()}
                </div>
                <div className="detail-genre">{selectedAlbum.genre}</div>
                <h2 id="detail-heading" className="detail-title">{selectedAlbum.album}</h2>
                <div className="detail-artist">{selectedAlbum.artist}</div>

                <div className="detail-facts">
                  <div className="detail-fact">
                    <span className="detail-fact-label">Released</span>
                    <span className="detail-fact-value">{selectedAlbum.year}</span>
                  </div>
                  <div className="detail-fact">
                    <span className="detail-fact-label">Tracks</span>
                    <span className="detail-fact-value">{selectedAlbum.tracks}</span>
                  </div>
                  <div className="detail-fact">
                    <span className="detail-fact-label">Length</span>
                    <span className="detail-fact-value">{Number(selectedAlbum.minutes ?? 0).toFixed(1)} min</span>
                  </div>
                </div>

                <div className="track-heading">
                  <h3>Track details</h3>
                  <span className="lr-count">{selectedTracks.length} tracks</span>
                </div>

                {selectedTracks.length === 0 ? (
                  <div className="empty">Track details are not available for this album in the current result.</div>
                ) : (
                  <div className="track-list" data-bow-viz="FIXTURE_VIZ_1">
                    {selectedTracks.map((track, index) => (
                      <div className="track-row" key={track.track_id}>
                        <span className="track-number">{String(index + 1).padStart(2, '0')}</span>
                        <span>{track.track}</span>
                        <span className="track-time">{Number(track.minutes ?? 0).toFixed(2)} min</span>
                      </div>
                    ))}
                  </div>
                )}

                <p className="lr-note">
                  Track rows are shown from the filtered catalog. Listening controls are intentionally absent:
                  this room is for browsing, not playback.
                </p>
              </div>
            )}
          </aside>
        </main>
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>