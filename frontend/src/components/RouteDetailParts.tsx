/* eslint-disable react-refresh/only-export-components -- detail helpers share the feature contract. */
import type { Route } from '../discovery/types'
import { trackProductEvent } from '../analytics'
import type { Copy, Language } from '../i18n/types'

export type ElevationPoint = { distance_m: number; elevation_m: number }

export function formatMetric(value: string | null | undefined, suffix: string): string {
  if (!value) return '—'
  const number = Number(value)
  if (!Number.isFinite(number)) return '—'
  return `${Math.round(number)} ${suffix}`
}

export function hasMetric(value: string | null | undefined): value is string {
  return value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value))
}

export function routeStatus(status: string, copy: Copy): string {
  if (status === 'verified') return copy.verifiedSource
  if (status === 'available' || status === 'community') return copy.communitySource
  if (status === 'unavailable') return copy.unavailableSource
  return copy.unknownSource
}

export function ElevationProfile({ points, copy }: { points: ElevationPoint[]; copy: Copy }) {
  if (!points.length) return null
  const minimum = Math.min(...points.map((point) => point.elevation_m))
  const maximum = Math.max(...points.map((point) => point.elevation_m))
  const distance = Math.max(points.at(-1)?.distance_m ?? 0, 1)
  const range = Math.max(maximum - minimum, 1)
  const coordinates = points
    .map((point) => {
      const x = (point.distance_m / distance) * 100
      const y = 96 - ((point.elevation_m - minimum) / range) * 86
      return `${x.toFixed(2)},${y.toFixed(2)}`
    })
    .join(' ')
  const formatDistance = (value: number) => `${(value / 1000).toFixed(1)} km`
  const formatElevation = (value: number) => `${Math.round(value)} m`
  return (
    <figure className="elevation-profile" aria-labelledby="elevation-profile-title">
      <figcaption id="elevation-profile-title">{copy.elevationProfile}</figcaption>
      <svg viewBox="0 0 100 100" role="img" aria-labelledby="elevation-profile-title">
        <polyline points={coordinates} />
      </svg>
      <ol aria-label={copy.elevationProfile}>
        {points.map((point, index) => (
          <li key={`${point.distance_m}-${point.elevation_m}-${index}`}>
            <span>{formatDistance(point.distance_m)}</span>
            <span>{formatElevation(point.elevation_m)}</span>
          </li>
        ))}
      </ol>
    </figure>
  )
}

export function RouteSources({
  route,
  copy,
  language,
}: {
  route: Route
  copy: Copy
  language: Language
}) {
  const dateFormatter = new Intl.DateTimeFormat(language === 'cs' ? 'cs-CZ' : 'en-GB', {
    dateStyle: 'medium',
  })
  return (
    <div className="source-section">
      <h3>{copy.sources}</h3>
      {route.sources.length ? (
        <ul className="source-list">
          {route.sources.map((source) => (
            <li key={source.mapy_url}>
              <a
                href={source.mapy_url}
                target="_blank"
                rel="noreferrer"
                aria-label={copy.mapyLink}
                onClick={() => trackProductEvent('original_source_click')}
              >
                {source.title || copy.mapyLink}
              </a>
              <span>{routeStatus(source.status, copy)}</span>
              {source.last_successful_check_at ? (
                <time dateTime={source.last_successful_check_at}>
                  {copy.sourceLastSuccessfulCheck(
                    dateFormatter.format(new Date(source.last_successful_check_at)),
                  )}
                </time>
              ) : null}
              {source.last_checked_at &&
              source.last_checked_at !== source.last_successful_check_at ? (
                <time dateTime={source.last_checked_at}>
                  {copy.sourceChecked(dateFormatter.format(new Date(source.last_checked_at)))}
                </time>
              ) : null}
              {source.posts.map((post) => (
                <span key={post.url} className="source-post">
                  <a
                    href={post.url}
                    target="_blank"
                    rel="noreferrer"
                    aria-label={copy.sourceLink(post.thread_title)}
                    onClick={() => trackProductEvent('original_source_click')}
                  >
                    {post.thread_title || post.url}
                  </a>
                  {post.author && <span> · {post.author}</span>}
                  {post.posted_at && (
                    <time dateTime={post.posted_at}>
                      {' · '}
                      {copy.posted(dateFormatter.format(new Date(post.posted_at)))}
                    </time>
                  )}
                </span>
              ))}
            </li>
          ))}
        </ul>
      ) : (
        <p className="no-sources">{copy.noSources}</p>
      )}
    </div>
  )
}
