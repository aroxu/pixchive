export const defaultArtworkSort = "created";

export const filterKeys = [
  "q",
  "artist",
  "tag_id",
  "tag",
  "exclude_tag",
  "tag_match",
  "tag_source",
  "kind",
  "rating",
  "visibility",
  "archive_status",
  "remote_status",
  "bookmark_status",
  "created_from",
  "created_to",
  "collected_from",
  "collected_to",
];

export function apiParams(params: URLSearchParams) {
  const result = new URLSearchParams(params);
  result.delete("view");
  if (!result.has("sort")) result.set("sort", defaultArtworkSort);
  return result;
}

export function clearFilters(params: URLSearchParams) {
  const result = new URLSearchParams(params);
  filterKeys.forEach((key) => result.delete(key));
  result.delete("page");
  return result;
}

export type LibraryScope = { artist?: string; tag_id?: string };

export function scopedParams(params: URLSearchParams, scope: LibraryScope) {
  const next = apiParams(params);
  for (const [key, value] of Object.entries(scope)) next.set(key, value);
  return next;
}

export function listingContext(origin: unknown) {
  const fallback = {
    path: "/library",
    params: new URLSearchParams(),
    scope: {} as LibraryScope,
  };
  if (typeof origin !== "string") return fallback;
  const [path, query = ""] = origin.split("?", 2);
  if (path === "/library")
    return {
      path,
      params: new URLSearchParams(query),
      scope: {} as LibraryScope,
    };
  const artist = path.match(/^\/artists\/([0-9]{1,20})$/);
  if (artist)
    return {
      path,
      params: new URLSearchParams(query),
      scope: { artist: artist[1] },
    };
  const tag = path.match(/^\/tags\/([1-9][0-9]*)$/);
  if (tag)
    return {
      path,
      params: new URLSearchParams(query),
      scope: { tag_id: tag[1] },
    };
  return fallback;
}

export function rememberLibrary(
  url: string,
  id: string,
  scroll = window.scrollY,
) {
  try {
    sessionStorage.setItem(`library:${url}`, JSON.stringify({ id, scroll }));
  } catch {
    // Navigation remains available when browser session storage is unavailable.
  }
}

export function libraryPosition(
  url: string,
): { id: string; scroll: number } | null {
  try {
    return JSON.parse(sessionStorage.getItem(`library:${url}`) ?? "null");
  } catch {
    return null;
  }
}
