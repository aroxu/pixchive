import { Input } from "@heroui/react";
import { useQuery } from "@tanstack/react-query";
import { ArrowUpRight, Hash, UserRound } from "lucide-react";
import { useEffect, useRef } from "react";
import {
  Link,
  useLocation,
  useNavigationType,
  useSearchParams,
} from "react-router-dom";
import { api, type Models } from "../api/client";
import { ErrorState, Loading, PageHeading } from "../components/Common";
import { ko } from "../i18n/ko";
import { TagCreateForm, TagRenameButton } from "../components/TagManagement";
import { PageNavigation } from "../components/PageNavigation";
import { libraryPosition, rememberLibrary } from "../api/library";
import { useDebouncedValue } from "../hooks/useDebouncedValue";
import { formatCount } from "../i18n/format";

export function Directory({ kind }: { kind: "artists" | "tags" }) {
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const navigationType = useNavigationType();
  const restored = useRef("");
  const focusResults = useRef(false);
  const from = `${location.pathname}${location.search}`;
  const search = params.get("q") ?? "";
  const debounced = useDebouncedValue(search);
  const waiting = search !== debounced;
  const sort = params.get("sort") === "count" ? "count" : "name";
  const source =
    kind === "tags" && Object.hasOwn(ko.sources, params.get("source") ?? "")
      ? params.get("source")!
      : "";
  const parsedPage = Number(params.get("page") ?? 1);
  const page =
    Number.isInteger(parsedPage) && parsedPage >= 1 && parsedPage <= 1_000_000
      ? parsedPage
      : 1;
  const request = new URLSearchParams({
    q: debounced,
    sort,
    page: String(page),
    page_size: "24",
  });
  if (source) request.set("source", source);
  const query = useQuery({
    queryKey: [kind, "pages", request.toString()],
    queryFn: ({ signal }) =>
      api<Models["ArtistList"] | Models["TagList"]>(
        `${kind === "tags" ? "/tags/catalog/pages" : "/artists/pages"}?${request}`,
        { signal },
      ),
    enabled: !waiting,
  });
  const items = query.data?.items ?? [];
  useEffect(() => {
    if (!waiting && query.data && query.data.page !== page) {
      const actual = query.data.page;
      setParams(
        (current) => {
          const next = new URLSearchParams(current);
          next.set("page", String(actual));
          return next;
        },
        { replace: true },
      );
    }
  }, [query.data, page, waiting, setParams]);
  useEffect(() => {
    if (
      waiting ||
      !query.data ||
      query.error ||
      restored.current === location.key
    )
      return;
    const frame = requestAnimationFrame(() => {
      restored.current = location.key;
      const saved = libraryPosition(`directory:${from}`);
      if (
        saved &&
        (navigationType === "POP" || location.state?.restoreDirectory)
      ) {
        (
          document.getElementById(saved.id) ??
          document.getElementById("directory-results")
        )?.focus({ preventScroll: true });
        window.scrollTo(0, saved.scroll);
      } else {
        window.scrollTo(0, 0);
        if (focusResults.current)
          document
            .getElementById("directory-results")
            ?.focus({ preventScroll: true });
      }
      focusResults.current = false;
    });
    return () => cancelAnimationFrame(frame);
  }, [
    query.data,
    query.error,
    waiting,
    location.key,
    location.state,
    navigationType,
    from,
  ]);
  const update = (key: string, value: string, replace = false) => {
    focusResults.current = key === "page";
    setParams(
      (current) => {
        const next = new URLSearchParams(current);
        if (value) next.set(key, value);
        else next.delete(key);
        if (key !== "page") next.delete("page");
        return next;
      },
      { replace },
    );
  };
  return (
    <>
      <PageHeading
        title={ko[kind]}
        description={
          kind === "artists" ? ko.artistDescription : ko.tagDescription
        }
      />
      <div className="directory-search">
        {kind === "tags" && <TagCreateForm />}
        <Input
          id={
            kind === "tags" ? "tag-catalog-search" : "artist-directory-search"
          }
          aria-label={
            kind === "artists" ? ko.directorySearch : ko.directoryTagSearch
          }
          placeholder={
            kind === "artists" ? ko.directorySearch : ko.directoryTagSearch
          }
          value={search}
          onChange={(e) => update("q", e.target.value, true)}
          maxLength={100}
        />
      </div>
      <div className="directory-controls">
        <label className="filter-field">
          {ko.sort}
          <select
            value={sort}
            onChange={(event) => update("sort", event.target.value)}
          >
            <option value="name">{ko.directorySortName}</option>
            <option value="count">{ko.directorySortCount}</option>
          </select>
        </label>
        {kind === "tags" && (
          <label className="filter-field">
            {ko.filterLabels.tag_source}
            <select
              value={source}
              onChange={(event) => update("source", event.target.value)}
            >
              <option value="">{ko.all}</option>
              {Object.entries(ko.sources).map(([value, name]) => (
                <option key={value} value={value}>
                  {name}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>
      <div
        id="directory-results"
        tabIndex={-1}
        aria-busy={waiting || query.isPending}
      >
        {!waiting && !query.error && query.data && (
          <p className="section-description directory-count" aria-live="polite">
            {kind === "artists"
              ? ko.artistDirectoryCount(formatCount(query.data.total))
              : ko.tagDirectoryCount(formatCount(query.data.total))}
          </p>
        )}
        {waiting || query.isPending ? (
          <Loading />
        ) : query.error ? (
          <ErrorState error={query.error} retry={() => void query.refetch()} />
        ) : !items.length ? (
          <div className="empty-state">
            <p>
              {search.trim() || source
                ? ko.noDirectoryResults
                : kind === "artists"
                  ? ko.emptyArtists
                  : ko.emptyTags}
            </p>
          </div>
        ) : (
          <div className="directory-grid">
            {items.map((item) => (
              <div
                className="directory-entry"
                key={`${item.id}-${"source" in item ? item.source : ""}`}
              >
                <Link
                  id={`directory-${kind}-${item.id}-${"source" in item ? item.source : "artist"}`}
                  className="directory-card"
                  to={
                    kind === "artists"
                      ? `/artists/${item.id}`
                      : `/tags/${item.id}?tag_source=${"source" in item ? item.source : ""}`
                  }
                  state={{ directoryFrom: from }}
                  onClick={(event) =>
                    rememberLibrary(`directory:${from}`, event.currentTarget.id)
                  }
                >
                  <span className="directory-icon">
                    {"avatar_url" in item && item.avatar_url ? (
                      <img
                        src={item.avatar_url}
                        alt={ko.avatarAlt(item.name)}
                        loading="lazy"
                      />
                    ) : kind === "artists" ? (
                      <UserRound size={21} />
                    ) : (
                      <Hash size={21} />
                    )}
                  </span>
                  <div>
                    <h2>{item.name}</h2>
                    {"alias" in item && item.alias && (
                      <span className="artist-alias">{item.alias}</span>
                    )}
                    {"account" in item && item.account && (
                      <small className="directory-translation">
                        {item.account}
                      </small>
                    )}
                    {"translations" in item && item.translations.length > 0 && (
                      <small className="directory-translation">
                        {item.translations
                          .map((translation) => translation.name)
                          .join(" · ")}
                      </small>
                    )}
                    <p>
                      {ko.count(item.artwork_count)}
                      {"source" in item && ` · ${ko.sources[item.source]}`}
                    </p>
                  </div>
                  <ArrowUpRight size={17} />
                </Link>
                {"source" in item &&
                  item.source === "local" &&
                  (item.editable ? (
                    <TagRenameButton tag={item} />
                  ) : (
                    <span className="tag-shared" title={ko.sharedTagHelp}>
                      {ko.sharedTag}
                    </span>
                  ))}
              </div>
            ))}
          </div>
        )}
        {!waiting && !query.error && query.data && (
          <PageNavigation
            page={query.data.page}
            pageSize={query.data.page_size}
            total={query.data.total}
            label={ko[kind]}
            onChange={(next) => update("page", String(next))}
          />
        )}
      </div>
    </>
  );
}
