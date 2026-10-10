import { Button, Input, Label, TextField } from "@heroui/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  ArrowUpRight,
  ChevronLeft,
  ChevronRight,
  Maximize,
  X,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { api, newIdempotencyKey, type Models } from "../api/client";
import { ErrorState, Loading, LocalImage } from "../components/Common";
import {
  libraryPosition,
  listingContext,
  rememberLibrary,
  scopedParams,
} from "../api/library";
import { TagTranslations } from "../components/TagTranslations";
import { FullscreenViewer } from "../components/FullscreenViewer";
import { LocalMetadata } from "../components/LocalMetadata";
import { ArtworkFiles } from "../components/ArtworkFiles";
import { ArtworkInformation } from "../components/ArtworkInformation";
import { useArtworkTagEdit } from "../hooks/useArtworkTagEdit";
import {
  ArtworkActions,
  ArtworkFacts,
  BookmarkInfo,
  RemoteHistory,
} from "../components/ArtworkMaintenance";
import { ko } from "../i18n/ko";

export function Detail() {
  const { id } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const context = listingContext(location.state?.from);
  const filters = context.params;
  const from = `${context.path}${filters.size ? `?${filters}` : ""}`;
  const requestParams = scopedParams(filters, context.scope);
  const neighbors = useQuery({
    queryKey: ["neighbors", id, requestParams.toString()],
    queryFn: () =>
      api<Models["ArtworkNeighbors"]>(
        `/artworks/${id}/neighbors?${requestParams}`,
      ),
  });
  const moveWork = (direction: "previous" | "next") => {
    const target = neighbors.data?.[direction];
    if (!target || !neighbors.data?.position) return;
    const position = neighbors.data.position + (direction === "next" ? 1 : -1);
    const targetPage = Math.ceil(
      position / Number(filters.get("page_size") ?? 24),
    );
    if (targetPage !== Number(filters.get("page") ?? 1))
      filters.set("page", String(targetPage));
    const targetFrom = `${context.path}${filters.size ? `?${filters}` : ""}`;
    rememberLibrary(
      targetFrom,
      target,
      libraryPosition(targetFrom)?.scroll ?? 0,
    );
    navigate(`/artworks/${target}`, {
      replace: true,
      state: { from: targetFrom, directoryFrom: location.state?.directoryFrom },
    });
  };
  const [page, setPage] = useState(0);
  const [tag, setTag] = useState("");
  const [videoError, setVideoError] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["artwork", id],
    queryFn: () => api<Models["ArtworkDetail"]>(`/artworks/${id}`),
    refetchInterval: (query) =>
      query.state.data?.kind === "ugoira" &&
      query.state.data.conversion_state !== "ready"
        ? 3000
        : false,
  });
  const convert = useMutation({
    mutationFn: () =>
      api<Models["JobView"]>(`/artworks/${id}/convert`, {
        method: "POST",
        headers: { "Idempotency-Key": newIdempotencyKey() },
      }),
    onSuccess: () => {
      setVideoError(false);
      void client.invalidateQueries({ queryKey: ["artwork", id] });
      void client.invalidateQueries({ queryKey: ["jobs"] });
    },
  });
  const edit = useArtworkTagEdit((request) => {
    if (request.action === "add" && request.artwork_ids.includes(id ?? ""))
      setTag((current) => (current === request.name ? "" : current));
  });
  useEffect(() => {
    setPage(0);
    setVideoError(false);
    window.scrollTo(0, 0);
  }, [id]);
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (
        e.target instanceof HTMLInputElement ||
        e.target instanceof HTMLTextAreaElement ||
        e.target instanceof HTMLVideoElement ||
        e.target instanceof HTMLSelectElement ||
        (e.target instanceof HTMLElement && e.target.isContentEditable) ||
        dialog.current?.open
      )
        return;
      if (e.key === "ArrowLeft") {
        e.preventDefault();
        setPage((p) => Math.max(0, p - 1));
      }
      if (e.key === "ArrowRight") {
        e.preventDefault();
        setPage((p) => Math.min((query.data?.pages.length ?? 1) - 1, p + 1));
      }
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [query.data?.pages.length]);
  if (query.isPending) return <Loading />;
  if (query.error)
    return (
      <ErrorState error={query.error} retry={() => void query.refetch()} />
    );
  const artwork = query.data;
  const image = artwork.pages[page];
  const controls = (
    <div className="viewer-controls">
      <Button
        variant="ghost"
        isIconOnly
        aria-label={ko.previous}
        isDisabled={page === 0}
        onPress={() => setPage((p) => p - 1)}
      >
        <ChevronLeft size={20} />
      </Button>
      <span>
        {ko.pagePosition(image ? image.index + 1 : 0, artwork.page_count)}
      </span>
      <Button
        variant="ghost"
        isIconOnly
        aria-label={ko.next}
        isDisabled={page >= artwork.pages.length - 1}
        onPress={() => setPage((p) => p + 1)}
      >
        <ChevronRight size={20} />
      </Button>
    </div>
  );
  return (
    <>
      <div className="detail-navigation">
        <Button
          className="back-button"
          variant="ghost"
          onPress={() => {
            rememberLibrary(
              from,
              artwork.id,
              libraryPosition(from)?.scroll ?? 0,
            );
            navigate(from, {
              state: {
                restore: true,
                directoryFrom: location.state?.directoryFrom,
              },
            });
          }}
        >
          <ArrowLeft size={17} />
          {context.path === "/library" ? ko.backLibrary : ko.backCollection}
        </Button>
        <div className="work-navigation" aria-label={ko.library}>
          <Button
            variant="secondary"
            isDisabled={!neighbors.data?.previous}
            onPress={() => moveWork("previous")}
          >
            <ChevronLeft size={16} />
            {ko.previousWork}
          </Button>
          {neighbors.data?.position && (
            <span>
              {ko.pagePosition(neighbors.data.position, neighbors.data.total)}
            </span>
          )}
          <Button
            variant="secondary"
            isDisabled={!neighbors.data?.next}
            onPress={() => moveWork("next")}
          >
            {ko.nextWork}
            <ChevronRight size={16} />
          </Button>
        </div>
      </div>
      {neighbors.error && (
        <ErrorState
          error={neighbors.error}
          retry={() => void neighbors.refetch()}
        />
      )}
      {neighbors.data?.position === null && (
        <p className="notice">{ko.outsideResults}</p>
      )}
      <div className="detail-layout">
        <section className="viewer">
          <div className="viewer-image">
            {artwork.playback_url && !videoError ? (
              <video
                key={artwork.playback_url}
                src={artwork.playback_url}
                poster={artwork.thumbnail_url ?? undefined}
                controls
                loop
                playsInline
                preload="metadata"
                aria-label={`${artwork.title} ${ko.playAnimation}`}
                onError={() => setVideoError(true)}
              />
            ) : (
              <>
                <LocalImage
                  key={image?.url}
                  src={image?.url ?? artwork.thumbnail_url}
                  alt={artwork.title}
                />
                <Button
                  className="zoom-button"
                  variant="secondary"
                  isIconOnly
                  aria-label={ko.zoom}
                  onPress={() => dialog.current?.showModal()}
                >
                  <Maximize size={18} />
                </Button>
              </>
            )}
          </div>
          {artwork.kind !== "ugoira" && controls}
          {videoError && (
            <p className="notice" role="status">
              {ko.playbackFailed}
            </p>
          )}
        </section>
        <aside className="detail-info">
          <span className="eyebrow">{ko.metadata}</span>
          <h1 id="artwork-title" tabIndex={-1}>
            {artwork.title}
          </h1>
          <Link className="detail-artist" to={`/artists/${artwork.artist_id}`}>
            {artwork.artist_avatar_url && (
              <img
                className="artist-avatar"
                src={artwork.artist_avatar_url}
                alt=""
                loading="lazy"
              />
            )}
            {artwork.artist_name}
            {artwork.artist_alias && ` · ${artwork.artist_alias}`}
            <ArrowUpRight size={16} />
          </Link>
          {artwork.artist_previous_names.length > 0 && (
            <p className="section-description">
              {ko.previousNames}: {artwork.artist_previous_names.join(", ")}
            </p>
          )}
          <div className="detail-status">
            <span className="status-dot" />
            {ko.archiveStates[artwork.archive_status]}
            <span>
              {artwork.kind === "ugoira"
                ? ko.ugoira
                : ko.pages(artwork.page_count)}
            </span>
          </div>
          {artwork.is_fixture && <p className="notice">{ko.sampleNotice}</p>}
          {artwork.kind === "ugoira" && (
            <section>
              <p className="section-description" role="status">
                {ko.conversionStates[artwork.conversion_state ?? "pending"] ??
                  ko.unknownState}
              </p>
              {artwork.conversion_error && (
                <p className="status-warning">
                  {ko.jobErrors[artwork.conversion_error] ?? ko.requestFailed}
                </p>
              )}
              <div className="form-actions">
                <Button
                  variant="secondary"
                  isPending={convert.isPending}
                  isDisabled={
                    !artwork.archive_url ||
                    artwork.conversion_state === "running"
                  }
                  onPress={() => convert.mutate()}
                >
                  {ko.createPlayback}
                </Button>
                {artwork.archive_url && (
                  <a
                    className="text-link"
                    href={artwork.archive_url}
                    download={`ugoira-${artwork.id}.zip`}
                  >
                    {ko.originalZip}
                  </a>
                )}
                {artwork.timing_url && (
                  <a
                    className="text-link"
                    href={artwork.timing_url}
                    download={`ugoira-${artwork.id}-frames.json`}
                  >
                    {ko.frameTiming}
                  </a>
                )}
              </div>
              {convert.error && <ErrorState error={convert.error} />}
              {convert.isSuccess && (
                <Link className="text-link" to={`/jobs/${convert.data.id}`}>
                  {ko.openJob}
                </Link>
              )}
            </section>
          )}
          <ArtworkInformation key={`information-${artwork.id}`}>
            {(informationActive) => (
              <>
                <section>
                  <h2>{ko.description}</h2>
                  <p className="caption">
                    {artwork.caption || ko.noDescription}
                  </p>
                </section>
                <section>
                  <h2>{ko.tags}</h2>
                  <div className="detail-tags">
                    {artwork.tags.map((t) => (
                      <div className="detail-tag" key={t.id}>
                        <Link to={`/tags/${t.id}`}>{t.name}</Link>
                        <span>
                          {t.sources
                            .map((source) => ko.sources[source])
                            .join(" · ")}
                        </span>
                        <TagTranslations translations={t.translations} />
                        {t.sources.includes("local") && (
                          <Button
                            size="sm"
                            variant="ghost"
                            isIconOnly
                            aria-label={`${t.name} ${ko.remove}`}
                            isDisabled={edit.isPending}
                            onPress={() =>
                              edit.mutate({
                                artwork_ids: [artwork.id],
                                name: t.name,
                                action: "remove",
                              })
                            }
                          >
                            <X size={13} />
                          </Button>
                        )}
                      </div>
                    ))}
                  </div>
                  {!artwork.tags.length && <p>{ko.noTags}</p>}
                  <form
                    className="tag-form"
                    onSubmit={(e) => {
                      e.preventDefault();
                      if (edit.isPending) return;
                      edit.mutate({
                        artwork_ids: [artwork.id],
                        name: tag,
                        action: "add",
                      });
                    }}
                  >
                    <TextField
                      value={tag}
                      onChange={setTag}
                      isRequired
                      isDisabled={edit.isPending}
                    >
                      <Label>{ko.addTag}</Label>
                      <Input maxLength={100} placeholder={ko.tagName} />
                    </TextField>
                    <Button
                      type="submit"
                      variant="secondary"
                      isPending={edit.isPending}
                    >
                      {ko.add}
                    </Button>
                  </form>
                  {edit.error && <ErrorState error={edit.error} />}
                </section>
                <LocalMetadata key={artwork.id} artwork={artwork} />
                <ArtworkFacts artwork={artwork} />
                <BookmarkInfo artwork={artwork} />
                <RemoteHistory
                  key={`history-${artwork.id}`}
                  id={artwork.id}
                  active={informationActive}
                />
                <ArtworkFiles
                  key={`files-${artwork.id}`}
                  id={artwork.id}
                  active={informationActive}
                />
                {!artwork.is_fixture && (
                  <a
                    className="external-link"
                    href={`https://www.pixiv.net/artworks/${artwork.id}`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {ko.originalPixiv}
                    <ArrowUpRight size={16} />
                  </a>
                )}
                <ArtworkActions
                  key={`actions-${artwork.id}`}
                  artwork={artwork}
                  active={informationActive}
                  onDeleted={() =>
                    navigate(from, {
                      replace: true,
                      state: {
                        restore: true,
                        directoryFrom: location.state?.directoryFrom,
                      },
                    })
                  }
                />
              </>
            )}
          </ArtworkInformation>
        </aside>
      </div>
      <FullscreenViewer
        dialog={dialog}
        src={image?.url ?? artwork.thumbnail_url}
        title={artwork.title}
        controls={controls}
        previous={() => setPage((p) => Math.max(0, p - 1))}
        next={() =>
          setPage((p) => Math.max(0, Math.min(artwork.pages.length - 1, p + 1)))
        }
      />
    </>
  );
}
