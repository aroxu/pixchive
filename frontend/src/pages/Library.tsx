import { Button, Input } from "@heroui/react";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowRight,
  Bookmark,
  Search,
  SlidersHorizontal,
  X,
  Layers,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import {
  Link,
  useLocation,
  useNavigationType,
  useSearchParams,
} from "react-router-dom";
import { api, type Models } from "../api/client";
import {
  ErrorState,
  Loading,
  LocalImage,
  PageHeading,
} from "../components/Common";
import {
  clearFilters,
  defaultArtworkSort,
  filterKeys,
  libraryPosition,
  rememberLibrary,
  scopedParams,
  type LibraryScope,
} from "../api/library";
import { LibraryFilters, filterOptions } from "../components/LibraryFilters";
import { useArtworkTagEdit } from "../hooks/useArtworkTagEdit";
import { ko } from "../i18n/ko";

export function Library({
  scope = {},
  hideHeading = false,
}: {
  scope?: LibraryScope;
  hideHeading?: boolean;
}) {
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const navigationType = useNavigationType();
  const libraryUrl = `${location.pathname}${location.search}`;
  const directoryState = { directoryFrom: location.state?.directoryFrom };
  const restored = useRef("");
  const focusResults = useRef(false);
  const resultsHeading = useRef<HTMLSpanElement>(null);
  const submittedScope = useRef<string | null>(null);
  const currentSelectionScope = useRef("");
  const [search, setSearch] = useState(params.get("q") ?? "");
  const [selection, setSelection] = useState<string[]>([]);
  const [tagName, setTagName] = useState("");
  const [tagAction, setTagAction] = useState<"add" | "remove">("add");
  const filterDialog = useRef<HTMLDialogElement>(null);
  const requestParams = scopedParams(params, scope);
  const requestKey = requestParams.toString();
  const selectionScope = `${location.pathname}?${requestKey}`;
  currentSelectionScope.current = selectionScope;
  const lockedFields = Object.keys(scope);
  const scoped = lockedFields.length > 0;
  const query = useQuery({
    queryKey: ["artworks", requestKey],
    queryFn: () => api<Models["ArtworkList"]>(`/artworks?${requestParams}`),
  });
  const pageIds = query.data?.items.map((artwork) => artwork.id) ?? [];
  const selectedIds = pageIds.filter((id) => selection.includes(id));
  const allSelected =
    pageIds.length > 0 && selectedIds.length === pageIds.length;
  const edit = useArtworkTagEdit((request) => {
    if (
      submittedScope.current !== currentSelectionScope.current ||
      !resultsHeading.current
    )
      return;
    if (request.artwork_ids.some((id) => selectedIds.includes(id)))
      resultsHeading.current.focus({ preventScroll: true });
    setTagName((current) => (current === request.name ? "" : current));
    setSelection((current) =>
      current.filter((id) => !request.artwork_ids.includes(id)),
    );
  });
  useEffect(() => {
    setSearch(params.get("q") ?? "");
  }, [params]);
  useEffect(() => {
    setSelection([]);
  }, [selectionScope]);
  useEffect(() => {
    if (!query.data || restored.current === location.key) return;
    const frame = requestAnimationFrame(() => {
      restored.current = location.key;
      const saved = libraryPosition(libraryUrl);
      if (saved && (navigationType === "POP" || location.state?.restore)) {
        document
          .getElementById(`artwork-${saved.id}`)
          ?.focus({ preventScroll: true });
        window.scrollTo(0, saved.scroll);
      } else {
        window.scrollTo(0, 0);
        if (focusResults.current)
          document
            .getElementById("library-results")
            ?.focus({ preventScroll: true });
      }
      focusResults.current = false;
    });
    return () => cancelAnimationFrame(frame);
  }, [query.data, libraryUrl, location.key, location.state, navigationType]);
  const change = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    value ? next.set(key, value) : next.delete(key);
    if (key !== "page" && key !== "view") next.delete("page");
    setParams(next, { state: directoryState });
  };
  const filtered = filterKeys.some(
    (key) =>
      key !== "tag_match" && !lockedFields.includes(key) && params.has(key),
  );
  const clear = () =>
    setParams(clearFilters(params), { state: directoryState });
  const apply = (next: URLSearchParams) => {
    focusResults.current = true;
    setParams(next, { state: directoryState });
    filterDialog.current?.close();
  };
  const sorting = (
    <label className="sort-label">
      {ko.sort}
      <select
        aria-label={ko.sort}
        value={params.get("sort") ?? defaultArtworkSort}
        onChange={(event) => change("sort", event.target.value)}
      >
        <option value="created">{ko.created}</option>
        <option value="collected">{ko.collected}</option>
        <option value="title">{ko.titleSort}</option>
        <option value="artist">{ko.artistSort}</option>
      </select>
    </label>
  );
  return (
    <>
      {!hideHeading && (
        <PageHeading
          eyebrow={ko.collection}
          title={ko.library}
          description={ko.libraryDescription}
        />
      )}
      <form
        className="search-bar"
        onSubmit={(e) => {
          e.preventDefault();
          change("q", search);
        }}
      >
        <Search size={20} />
        <Input
          aria-label={ko.search}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder={ko.searchPlaceholder}
          maxLength={300}
        />
        <Button variant="secondary" type="submit">
          {ko.searchButton}
        </Button>
        <Button
          className="mobile-filter-button"
          variant="ghost"
          isIconOnly
          aria-label={ko.filter}
          onPress={() => filterDialog.current?.showModal()}
        >
          <SlidersHorizontal size={19} />
        </Button>
      </form>
      <p className="field-help search-help">{ko.searchHelp}</p>
      <div className="library-layout">
        <aside className="desktop-filters" aria-label={ko.filter}>
          <h2>{ko.filter}</h2>
          <LibraryFilters
            key={params.toString()}
            params={params}
            apply={apply}
            lockedFields={lockedFields}
          />
        </aside>
        <div className="library-results">
          <div className="library-toolbar">
            <div className="library-tabs">
              <span
                ref={resultsHeading}
                className="library-tab"
                id="library-results"
                tabIndex={-1}
              >
                {ko.allArtworks}
              </span>
              {query.data && (
                <span className="result-count" role="status">
                  {ko.count(query.data.total)}
                </span>
              )}
            </div>
            <div className="library-view-options">
              {sorting}
              <label className="sort-label">
                {ko.cardSize}
                <select
                  value={params.get("view") ?? "medium"}
                  onChange={(e) => change("view", e.target.value)}
                >
                  {Object.entries(ko.cardSizes).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="sort-label">
                {ko.pageSize}
                <select
                  value={params.get("page_size") ?? "24"}
                  onChange={(e) => change("page_size", e.target.value)}
                >
                  {[12, 24, 48, 96].map((size) => (
                    <option key={size} value={size}>
                      {size}
                    </option>
                  ))}
                </select>
              </label>
            </div>
          </div>
          {filtered && (
            <div className="filter-chips">
              {Array.from(params.entries())
                .filter(
                  ([key]) =>
                    filterKeys.includes(key) && !lockedFields.includes(key),
                )
                .map(([key, value], index) => (
                  <button
                    key={`${key}-${index}`}
                    aria-label={`${ko.filterLabels[key]} ${value} ${ko.remove}`}
                    onClick={() => {
                      const next = new URLSearchParams(params);
                      next.delete(key, value);
                      next.delete("page");
                      if (!next.has("tag")) next.delete("tag_match");
                      setParams(next, { state: directoryState });
                    }}
                  >
                    {ko.filterLabels[key]}:{" "}
                    {filterOptions[key]?.[value] ?? value}
                    <X size={13} />
                  </button>
                ))}
              <Button size="sm" variant="ghost" onPress={clear}>
                {ko.clearFilters}
              </Button>
            </div>
          )}
          {edit.error && <ErrorState error={edit.error} />}
          {edit.isSuccess && (
            <p className="saved-message" role="status">
              {ko.bulkApplied(
                edit.variables.artwork_ids.length,
                edit.variables.action,
              )}
            </p>
          )}
          {query.isPending ? (
            <Loading />
          ) : query.error ? (
            <ErrorState
              error={query.error}
              retry={() => void query.refetch()}
            />
          ) : query.data.total === 0 ? (
            <section className="empty-state">
              <div className="empty-icon">
                <Bookmark size={31} />
              </div>
              <h2>
                {filtered
                  ? ko.noResults
                  : scoped
                    ? ko.emptyCollection
                    : ko.emptyTitle}
              </h2>
              <p>
                {filtered
                  ? ko.noResultsDescription
                  : scoped
                    ? ko.emptyCollectionHelp
                    : ko.emptyDescription}
              </p>
              {filtered ? (
                <Button variant="secondary" onPress={clear}>
                  {ko.clearFilters}
                </Button>
              ) : scoped ? (
                <Link className="action-link" to="/library">
                  {ko.backLibrary}
                  <ArrowRight size={16} />
                </Link>
              ) : (
                <Link className="action-link" to="/settings">
                  {ko.goSettings}
                  <ArrowRight size={16} />
                </Link>
              )}
            </section>
          ) : (
            <>
              <div className="page-selection">
                <Button
                  size="sm"
                  variant="secondary"
                  aria-pressed={allSelected}
                  aria-describedby="page-selection-help"
                  isDisabled={edit.isPending || query.isFetching}
                  onPress={() => {
                    edit.reset();
                    setSelection(allSelected ? [] : [...pageIds]);
                  }}
                >
                  {allSelected
                    ? ko.deselectPage
                    : ko.selectPage(pageIds.length)}
                </Button>
                <p className="field-help" id="page-selection-help">
                  {ko.selectPageHelp}
                </p>
              </div>
              <div
                className={`artwork-grid view-${params.get("view") ?? "medium"}`}
              >
                {query.data.items.map((artwork) => (
                  <article className="artwork-card" key={artwork.id}>
                    <div className="artwork-image">
                      <Link
                        id={`artwork-${artwork.id}`}
                        to={`/artworks/${artwork.id}`}
                        state={{ ...directoryState, from: libraryUrl }}
                        onClick={() => rememberLibrary(libraryUrl, artwork.id)}
                      >
                        <LocalImage
                          src={artwork.thumbnail_url}
                          alt={artwork.title}
                        />
                      </Link>
                      <input
                        className="artwork-select"
                        type="checkbox"
                        aria-label={ko.selectArtwork(artwork.title)}
                        checked={selectedIds.includes(artwork.id)}
                        disabled={edit.isPending || query.isFetching}
                        onChange={(e) => {
                          edit.reset();
                          setSelection((old) =>
                            e.target.checked
                              ? [...new Set([...old, artwork.id])]
                              : old.filter((id) => id !== artwork.id),
                          );
                        }}
                      />
                      {artwork.kind === "ugoira" && (
                        <span className="page-badge">{ko.ugoira}</span>
                      )}
                      {artwork.page_count > 1 && artwork.kind !== "ugoira" && (
                        <span className="page-badge">
                          <Layers size={12} />
                          {artwork.page_count}
                        </span>
                      )}
                      {artwork.is_fixture && (
                        <span className="fixture-badge">{ko.sample}</span>
                      )}
                    </div>
                    <div className="artwork-info">
                      <h2>
                        <Link
                          to={`/artworks/${artwork.id}`}
                          state={{ ...directoryState, from: libraryUrl }}
                          onClick={() =>
                            rememberLibrary(libraryUrl, artwork.id)
                          }
                        >
                          {artwork.title}
                        </Link>
                      </h2>
                      <Link
                        to={`/artists/${artwork.artist_id}`}
                        className="artist-link"
                      >
                        {artwork.artist_name}
                      </Link>
                      {artwork.archive_status !== "complete" && (
                        <span className="status-warning">
                          {ko.archiveStates[artwork.archive_status]}
                        </span>
                      )}
                    </div>
                  </article>
                ))}
              </div>
              <div className="pagination">
                <Button
                  variant="secondary"
                  isDisabled={query.data.page <= 1}
                  onPress={() => change("page", String(query.data.page - 1))}
                >
                  {ko.previous}
                </Button>
                <span>
                  {ko.pagePosition(
                    query.data.page,
                    Math.ceil(query.data.total / query.data.page_size),
                  )}
                </span>
                <Button
                  variant="secondary"
                  isDisabled={
                    query.data.page * query.data.page_size >= query.data.total
                  }
                  onPress={() => change("page", String(query.data.page + 1))}
                >
                  {ko.next}
                </Button>
              </div>
            </>
          )}
        </div>
      </div>
      {query.isSuccess && selectedIds.length > 0 && (
        <form
          className="selection-bar"
          onSubmit={(e) => {
            e.preventDefault();
            if (edit.isPending || query.isFetching || query.isError) return;
            submittedScope.current = selectionScope;
            edit.mutate({
              artwork_ids: [...selectedIds],
              name: tagName,
              action: tagAction,
            });
          }}
        >
          <strong>{ko.selected(selectedIds.length)}</strong>
          <Input
            aria-label={ko.tagName}
            value={tagName}
            onChange={(e) => {
              edit.reset();
              setTagName(e.target.value);
            }}
            disabled={edit.isPending || query.isFetching}
            maxLength={100}
            required
            placeholder={ko.addTag}
          />
          <select
            aria-label={ko.bulkAction}
            value={tagAction}
            onChange={(e) => {
              edit.reset();
              setTagAction(e.target.value as "add" | "remove");
            }}
            disabled={edit.isPending || query.isFetching}
          >
            {Object.entries(ko.bulkActions).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
          <Button
            type="submit"
            isPending={edit.isPending}
            isDisabled={query.isFetching}
          >
            {ko.bulkActions[tagAction]}
          </Button>
          <Button
            variant="ghost"
            isIconOnly
            aria-label={ko.deselect}
            isDisabled={edit.isPending}
            onPress={() => {
              edit.reset();
              setSelection([]);
            }}
          >
            <X size={18} />
          </Button>
        </form>
      )}
      <dialog
        ref={filterDialog}
        className="filter-dialog"
        aria-label={ko.filter}
      >
        <div className="dialog-heading">
          <h2>{ko.filter}</h2>
          <Button
            variant="ghost"
            isIconOnly
            aria-label={ko.close}
            onPress={() => filterDialog.current?.close()}
          >
            <X size={20} />
          </Button>
        </div>
        <LibraryFilters
          key={params.toString()}
          params={params}
          apply={apply}
          lockedFields={lockedFields}
        />
      </dialog>
    </>
  );
}
