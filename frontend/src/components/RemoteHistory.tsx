import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Models } from "../api/client";
import { ko } from "../i18n/ko";
import {
  formatCount,
  formatDateTime,
  formatRemoteDateTime,
} from "../i18n/format";
import { ErrorState, Loading } from "./Common";
import { PageNavigation } from "./PageNavigation";
import { TagTranslations } from "./TagTranslations";

function Snapshot({
  artworkId,
  id,
  active,
}: {
  artworkId: string;
  id: number;
  active: boolean;
}) {
  const [open, setOpen] = useState(false);
  const query = useQuery({
    queryKey: ["artwork-snapshot", artworkId, id],
    queryFn: () =>
      api<Models["HistorySnapshot"]>(`/artworks/${artworkId}/history/${id}`),
    enabled: active && open,
  });
  const snapshot = query.data;
  return (
    <details
      className="history-snapshot"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>{ko.historySnapshot}</summary>
      {open &&
        (query.isPending ? (
          <Loading />
        ) : query.error ? (
          <ErrorState error={query.error} retry={() => void query.refetch()} />
        ) : snapshot ? (
          <div className="history-snapshot-content">
            <dl className="status-list">
              <div>
                <dt>{ko.historyFields.title}</dt>
                <dd>{snapshot.title}</dd>
              </div>
              <div>
                <dt>{ko.historyFields.artist_name}</dt>
                <dd>
                  {snapshot.artist_name} · {snapshot.artist_id}
                </dd>
              </div>
              {snapshot.artist_account !== null && (
                <div>
                  <dt>{ko.artistAccount}</dt>
                  <dd>{snapshot.artist_account}</dd>
                </div>
              )}
              <div>
                <dt>{ko.filterLabels.kind}</dt>
                <dd>
                  {ko.kinds[snapshot.kind as keyof typeof ko.kinds] ??
                    ko.unknownState}
                </dd>
              </div>
              <div>
                <dt>{ko.filterLabels.rating}</dt>
                <dd>
                  {ko.ratings[snapshot.rating as keyof typeof ko.ratings] ??
                    ko.ratings.unknown}
                </dd>
              </div>
              <div>
                <dt>{ko.historyFields.page_count}</dt>
                <dd>{formatCount(snapshot.page_count)}</dd>
              </div>
              <div>
                <dt>{ko.publishedAt}</dt>
                <dd>
                  {formatRemoteDateTime(snapshot.created_at) ??
                    ko.noPublishedDate}
                </dd>
              </div>
              <div>
                <dt>{ko.bookmarkedAt}</dt>
                <dd>
                  {formatRemoteDateTime(snapshot.bookmarked_at) ??
                    ko.historyFieldUnknown}
                </dd>
              </div>
              <div>
                <dt>{ko.ratingRawValue}</dt>
                <dd>
                  {snapshot.rating_evidence ? (
                    <>
                      {ko.ratingSources[snapshot.rating_evidence.source]} ·{" "}
                      <code>
                        {JSON.stringify(snapshot.rating_evidence.value)}
                      </code>
                    </>
                  ) : (
                    ko.noRatingEvidence
                  )}
                </dd>
              </div>
            </dl>
            <h3>{ko.historyFields.caption}</h3>
            <p className="caption">{snapshot.caption || ko.noDescription}</p>
            <h3>{ko.sources.pixiv_work}</h3>
            {snapshot.work_tags === null ? (
              <p className="field-help">{ko.historyFieldUnknown}</p>
            ) : snapshot.work_tags.length === 0 ? (
              <p className="field-help">{ko.historyTagsEmpty}</p>
            ) : (
              <ul className="history-snapshot-tags">
                {snapshot.work_tags.map((tag, index) => (
                  <li key={`${index}-${tag.name}`}>
                    <strong>{tag.name}</strong>
                    <TagTranslations
                      translations={Object.entries(tag.translations).map(
                        ([language, name]) => ({
                          language,
                          name,
                          source: "pixiv",
                        }),
                      )}
                    />
                  </li>
                ))}
              </ul>
            )}
            <h3>{ko.sources.pixiv_bookmark}</h3>
            <p className="field-help">
              {snapshot.bookmark_tags === null
                ? ko.historyFieldUnknown
                : snapshot.bookmark_tags.length === 0
                  ? ko.historyTagsEmpty
                  : snapshot.bookmark_tags.join(" · ")}
            </p>
          </div>
        ) : null)}
    </details>
  );
}

export function RemoteHistory({
  id,
  active = true,
}: {
  id: string;
  active?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState(1);
  const query = useQuery({
    queryKey: ["artwork-history", id, page],
    queryFn: () =>
      api<Models["HistoryList"]>(
        `/artworks/${id}/history/pages?page=${page}&page_size=10`,
      ),
    enabled: active && open,
    refetchInterval: active && open ? 5000 : false,
  });
  return (
    <details
      className="job-issues history"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>{ko.history}</summary>
      {open && (
        <>
          <p className="section-description">{ko.historyHelp}</p>
          {query.isPending ? (
            <Loading />
          ) : query.error ? (
            <ErrorState
              error={query.error}
              retry={() => void query.refetch()}
            />
          ) : (
            <>
              <p className="section-description" aria-live="polite">
                {ko.historyCount(formatCount(query.data.total))}
              </p>
              {query.data.items.length === 0 && (
                <p className="field-help">{ko.noHistory}</p>
              )}
              <ul className="history-entries">
                {query.data.items.map((entry) => (
                  <li key={entry.id}>
                    <strong>{formatDateTime(entry.fetched_at)}</strong>
                    {entry.state === "available" ? (
                      <>
                        <span>
                          {entry.title} · {entry.artist_name}
                        </span>
                        <span>
                          {entry.initial
                            ? ko.historyInitial
                            : !entry.comparison_available
                              ? ko.historyComparisonUnknown
                              : entry.changes.length
                                ? entry.changes
                                    .map(
                                      (change) =>
                                        ko.historyFields[change] ?? change,
                                    )
                                    .join(" · ")
                                : ko.historyNoTracked}
                        </span>
                        {entry.tag_changes.map((group) => (
                          <div
                            className="history-tag-change"
                            key={group.source}
                          >
                            <strong>{ko.sources[group.source]}</strong>
                            {group.added.length > 0 && (
                              <small>
                                {ko.addedTags}: {group.added.join(", ")}
                              </small>
                            )}
                            {group.removed.length > 0 && (
                              <small>
                                {ko.removedTags}: {group.removed.join(", ")}
                              </small>
                            )}
                          </div>
                        ))}
                        <Snapshot
                          artworkId={id}
                          id={entry.id}
                          active={active && open}
                        />
                      </>
                    ) : (
                      <p className="field-help">
                        {entry.state === "hidden"
                          ? ko.historyHidden
                          : ko.historyUnreadable}
                      </p>
                    )}
                  </li>
                ))}
              </ul>
              <PageNavigation
                page={query.data.page}
                pageSize={query.data.page_size}
                total={query.data.total}
                label={ko.history}
                onChange={setPage}
              />
            </>
          )}
        </>
      )}
    </details>
  );
}
