import { Button } from "@heroui/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, RotateCcw, Trash2, X } from "lucide-react";
import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, newIdempotencyKey, type Models } from "../api/client";
import { ErrorState, Loading } from "./Common";
import { PreviewRegeneration } from "./PreviewRegeneration";
import { ko } from "../i18n/ko";
import { formatBytes, formatCount, formatDateTime } from "../i18n/format";

export { RemoteHistory } from "./RemoteHistory";

export function ArtworkFacts({
  artwork,
}: {
  artwork: Models["ArtworkDetail"];
}) {
  const published = artwork.created_at ? new Date(artwork.created_at) : null;
  const evidence = artwork.rating_evidence;
  return (
    <section>
      <h2>{ko.artworkFacts}</h2>
      <dl className="status-list">
        <div>
          <dt>{ko.artworkId}</dt>
          <dd>{artwork.id}</dd>
        </div>
        <div>
          <dt>{ko.filterLabels.rating}</dt>
          <dd>
            {(ko.ratings as Record<string, string>)[artwork.rating] ??
              ko.ratings.unknown}
          </dd>
        </div>
        <div>
          <dt>{ko.publishedAt}</dt>
          <dd>
            {published && !Number.isNaN(published.getTime())
              ? published.toLocaleString("ko-KR")
              : ko.noPublishedDate}
          </dd>
        </div>
        <div>
          <dt>{ko.collectedAt}</dt>
          <dd>{formatDateTime(artwork.collected_at)}</dd>
        </div>
        <div>
          <dt>{ko.artistNameAtCollection}</dt>
          <dd>{artwork.artist_name_at_collection}</dd>
        </div>
        {artwork.artist_account && (
          <div>
            <dt>{ko.artistAccount}</dt>
            <dd>{artwork.artist_account}</dd>
          </div>
        )}
      </dl>
      <details className="rating-evidence">
        <summary>{ko.ratingEvidence}</summary>
        <p className="field-help">{ko.ratingEvidenceHelp}</p>
        {evidence ? (
          <dl className="status-list">
            <div>
              <dt>{ko.ratingSource}</dt>
              <dd>{ko.ratingSources[evidence.source]}</dd>
            </div>
            <div>
              <dt>{ko.ratingRawValue}</dt>
              <dd>
                <code>{JSON.stringify(evidence.value)}</code>
              </dd>
            </div>
          </dl>
        ) : (
          <p className="section-description">{ko.noRatingEvidence}</p>
        )}
      </details>
    </section>
  );
}

export function BookmarkInfo({
  artwork,
}: {
  artwork: Models["ArtworkDetail"];
}) {
  const bookmark = artwork.bookmark;
  return (
    <section>
      <h2>{ko.bookmarkInfo}</h2>
      {bookmark ? (
        <dl className="status-list">
          <div>
            <dt>
              {ko.visibilities[bookmark.visibility] ?? bookmark.visibility}
            </dt>
            <dd>
              {ko.bookmarkStates[bookmark.status as "seen" | "not_seen"] ??
                ko.unknownState}
            </dd>
          </div>
          <div>
            <dt>{ko.lastSeen}</dt>
            <dd>{formatDateTime(bookmark.last_seen)}</dd>
          </div>
          <div>
            <dt>{ko.firstSeen}</dt>
            <dd>{formatDateTime(bookmark.first_seen)}</dd>
          </div>
          {bookmark.bookmarked_at && (
            <div>
              <dt>{ko.bookmarkedAt}</dt>
              <dd>
                {new Date(bookmark.bookmarked_at).toLocaleString("ko-KR")}
              </dd>
            </div>
          )}
        </dl>
      ) : (
        <p className="section-description">{ko.notBookmarked}</p>
      )}
      <dl className="status-list">
        <div>
          <dt>{ko.filterLabels.remote_status}</dt>
          <dd>
            {ko.remoteStates[
              artwork.remote_status as "available" | "unknown"
            ] ?? ko.unknownState}
          </dd>
        </div>
        {artwork.metadata_fetched_at && (
          <div>
            <dt>{ko.metadataFetched}</dt>
            <dd>{formatDateTime(artwork.metadata_fetched_at)}</dd>
          </div>
        )}
      </dl>
    </section>
  );
}

export function ArtworkActions({
  artwork,
  onDeleted,
  active = true,
}: {
  artwork: Models["ArtworkDetail"];
  onDeleted: () => void;
  active?: boolean;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const client = useQueryClient();
  const repair = useMutation({
    mutationFn: () =>
      api<Models["JobView"]>(`/artworks/${artwork.id}/repair`, {
        method: "POST",
        headers: { "Idempotency-Key": newIdempotencyKey() },
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["jobs"] });
    },
  });
  const preview = useQuery({
    queryKey: ["deletion", artwork.id],
    queryFn: () =>
      api<Models["DeletionPreview"]>(`/artworks/${artwork.id}/deletion`),
    enabled: open,
    staleTime: 0,
  });
  const remove = useMutation({
    mutationFn: () =>
      api<Models["JobView"]>(`/artworks/${artwork.id}/delete`, {
        method: "POST",
        headers: { "Idempotency-Key": newIdempotencyKey() },
      }),
    onSuccess: () => {
      dialog.current?.close();
      for (const key of [
        "artworks",
        "artists",
        "tags",
        "neighbors",
        "exclusions",
        "system",
        "jobs",
      ])
        void client.invalidateQueries({ queryKey: [key] });
      client.removeQueries({ queryKey: ["artwork", artwork.id] });
      onDeleted();
    },
  });
  const close = () => {
    dialog.current?.close();
  };
  return (
    <section className="artwork-actions">
      {artwork.kind !== "ugoira" && (
        <PreviewRegeneration
          key={artwork.id}
          artwork={artwork}
          active={active}
        />
      )}
      {!artwork.download_allowed && (
        <p className="notice">
          {ko.downloadNotSelected}{" "}
          <Link className="text-link" to="/settings">
            {ko.settings}
          </Link>
        </p>
      )}
      <Button
        variant="secondary"
        isPending={repair.isPending}
        isDisabled={!artwork.download_allowed}
        onPress={() => repair.mutate()}
      >
        <RotateCcw size={15} />
        {ko.repairArtwork}
      </Button>
      <p className="field-help">{ko.repairHelp}</p>
      {repair.error && <ErrorState error={repair.error} />}
      {repair.isSuccess && (
        <p className="saved-message" role="status">
          {ko.repairQueued}{" "}
          <Link className="text-link" to={`/jobs/${repair.data.id}`}>
            {ko.openJob}
          </Link>
        </p>
      )}
      <a
        className="text-link with-icon"
        href={`/api/artworks/${artwork.id}/export`}
        download={`pixchive-${artwork.id}.zip`}
      >
        <Download size={15} />
        {ko.exportArtwork}
      </a>
      <Button
        ref={trigger}
        variant="ghost"
        className="danger-button"
        onPress={() => {
          remove.reset();
          setOpen(true);
          dialog.current?.showModal();
        }}
      >
        <Trash2 size={15} />
        {ko.deleteArtwork}
      </Button>
      <dialog
        ref={dialog}
        className="confirm-dialog"
        aria-labelledby={`delete-title-${artwork.id}`}
        onClose={() => {
          setOpen(false);
          trigger.current?.focus();
        }}
      >
        <div className="dialog-heading">
          <h2 id={`delete-title-${artwork.id}`}>{ko.deleteTitle}</h2>
          <Button
            variant="ghost"
            isIconOnly
            aria-label={ko.close}
            onPress={close}
          >
            <X size={20} />
          </Button>
        </div>
        <p className="confirm-target">{artwork.title}</p>
        {preview.isPending ? (
          <Loading />
        ) : preview.error ? (
          <ErrorState
            error={preview.error}
            retry={() => void preview.refetch()}
          />
        ) : (
          <>
            <p>
              {ko.deleteFiles(
                formatCount(preview.data.file_count),
                formatBytes(preview.data.bytes),
              )}
            </p>
            {preview.data.roles.length > 0 && (
              <ul className="confirm-roles">
                {preview.data.roles.map((item) => (
                  <li key={item.role}>
                    {ko.roles[item.role] ?? item.role} {formatCount(item.files)}{" "}
                    · {formatBytes(item.bytes)}
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
        <p className="section-description">{ko.deleteKeep}</p>
        {remove.error && <ErrorState error={remove.error} />}
        <div className="form-actions dialog-actions">
          <Button variant="secondary" onPress={close}>
            {ko.cancel}
          </Button>
          <Button
            className="danger-solid"
            isPending={remove.isPending}
            isDisabled={!preview.data}
            onPress={() => remove.mutate()}
          >
            {ko.deleteConfirm}
          </Button>
        </div>
      </dialog>
    </section>
  );
}
