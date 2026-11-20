import { Button, Input, Label, TextField } from "@heroui/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bookmark, Check, Plus, Search, X } from "lucide-react";
import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { api, body, type Models } from "../api/client";
import { favoriteCollectionId } from "../api/library";
import { useDebouncedValue } from "../hooks/useDebouncedValue";
import { ko } from "../i18n/ko";
import { ErrorState, Loading } from "./Common";

type Artwork = Models["ArtworkSummary"];
type OpenPicker = (artwork: Artwork, trigger: HTMLButtonElement | null) => void;
const PickerContext = createContext<OpenPicker | null>(null);

export function useSaveRefresh() {
  const client = useQueryClient();
  return () =>
    Promise.all(
      [
        "artworks",
        "artwork",
        "neighbors",
        "saved-collections",
        "saved-collection",
        "artwork-saved",
        "favorite-summary",
      ].map((key) => client.invalidateQueries({ queryKey: [key] })),
    );
}

export function ArtworkSaveProvider({ children }: { children: ReactNode }) {
  const [target, setTarget] = useState<Artwork | null>(null);
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [name, setName] = useState("");
  const [message, setMessage] = useState("");
  const checkedSearch = useDebouncedValue(search);
  const dialog = useRef<HTMLDialogElement>(null);
  const returnFocus = useRef<HTMLButtonElement | null>(null);
  const client = useQueryClient();
  const refresh = useSaveRefresh();
  const saved = useQuery({
    queryKey: ["artwork-saved", target?.id],
    queryFn: () =>
      api<Models["ArtworkSaveState"]>(`/artworks/${target!.id}/saved`),
    enabled: target !== null,
    staleTime: 0,
  });
  const lists = useQuery({
    queryKey: ["saved-collections", checkedSearch, page],
    queryFn: () =>
      api<Models["SavedCollectionList"]>(
        `/collections?${new URLSearchParams({ q: checkedSearch, page: String(page), page_size: "20" })}`,
      ),
    enabled: target !== null,
  });
  useEffect(() => {
    if (!lists.data || lists.isFetching) return;
    const last = Math.max(
      1,
      Math.ceil(lists.data.total / lists.data.page_size),
    );
    if (page > last) setPage(last);
  }, [lists.data, lists.isFetching, page]);
  const membership = useMutation({
    mutationFn: (request: {
      collectionId: string;
      artworkId: string;
      included: boolean;
    }) =>
      request.collectionId === favoriteCollectionId
        ? api<Models["ArtworkSaveState"]>(
            `/artworks/${request.artworkId}/favorite`,
            { method: "PUT", body: body({ favorite: request.included }) },
          )
        : api<Models["ArtworkSaveState"]>(
            `/collections/${request.collectionId}/artworks/${request.artworkId}`,
            { method: request.included ? "PUT" : "DELETE" },
          ),
    onSuccess: async (state, request) => {
      client.setQueryData(["artwork-saved", request.artworkId], state);
      setMessage(
        request.included ? ko.collectionUpdated : ko.collectionRemoved,
      );
      await refresh();
    },
  });
  const create = useMutation({
    mutationFn: (request: { name: string; artwork_id: string }) =>
      api<Models["SavedCollectionView"]>("/collections", {
        method: "POST",
        body: body(request),
      }),
    onSuccess: async () => {
      setName("");
      setSearch("");
      setPage(1);
      setMessage(ko.collectionCreatedAndSaved);
      await refresh();
    },
  });
  const busy = membership.isPending || create.isPending;
  const changeMembership = (collectionId: string, included: boolean) => {
    if (!target || busy) return;
    setMessage("");
    membership.reset();
    membership.mutate({ collectionId, artworkId: target.id, included });
  };
  const open: OpenPicker = (artwork, trigger) => {
    returnFocus.current = trigger;
    setTarget(artwork);
    setSearch("");
    setPage(1);
    setName("");
    setMessage("");
    membership.reset();
    create.reset();
    dialog.current?.showModal();
  };
  return (
    <PickerContext.Provider value={open}>
      {children}
      <dialog
        ref={dialog}
        className="saved-collection-dialog"
        aria-labelledby="saved-collection-picker-title"
        aria-describedby="saved-collection-picker-help"
        onCancel={(event) => {
          if (busy) event.preventDefault();
        }}
        onClose={() => {
          setTarget(null);
          const trigger = returnFocus.current;
          if (trigger?.isConnected) trigger.focus();
          else
            (
              document.getElementById("library-results") ??
              document.getElementById("main")
            )?.focus();
        }}
      >
        <div className="sheet-handle" aria-hidden="true" />
        <div className="dialog-heading">
          <h2 id="saved-collection-picker-title">
            <Bookmark size={21} />
            {ko.chooseCollection}
          </h2>
          <Button
            variant="ghost"
            isIconOnly
            aria-label={ko.close}
            isDisabled={busy}
            onPress={() => dialog.current?.close()}
          >
            <X size={20} />
          </Button>
        </div>
        <div className="saved-picker-body">
          <p className="picker-artwork-title">{target?.title}</p>
          <p id="saved-collection-picker-help" className="section-description">
            {ko.chooseCollectionHelp}
          </p>
          {saved.isPending ? (
            <Loading />
          ) : saved.error ? (
            <ErrorState
              error={saved.error}
              retry={() => void saved.refetch()}
            />
          ) : (
            <>
              <label className="collection-picker-option system-collection-option">
                <input
                  type="checkbox"
                  aria-label={ko.collectionMembership(ko.favorites)}
                  checked={saved.data.favorite}
                  disabled={busy || saved.isFetching}
                  onChange={(event) =>
                    changeMembership(favoriteCollectionId, event.target.checked)
                  }
                />
                <Bookmark size={20} aria-hidden="true" />
                <span>
                  <strong>{ko.favorites}</strong>
                  <small>{ko.favoriteSystemShortHelp}</small>
                </span>
                <span className="system-collection-badge">
                  {ko.favoriteSystemLabel}
                </span>
              </label>
              <div className="collection-picker-section-heading">
                <h3>{ko.customCollections}</h3>
              </div>
              <div className="collection-picker-search">
                <Search size={18} aria-hidden="true" />
                <Input
                  aria-label={ko.collectionSearch}
                  placeholder={ko.collectionSearch}
                  value={search}
                  maxLength={100}
                  disabled={busy}
                  onChange={(event) => {
                    setSearch(event.target.value);
                    setPage(1);
                  }}
                />
              </div>
              {lists.isPending ? (
                <Loading />
              ) : lists.error ? (
                <ErrorState
                  error={lists.error}
                  retry={() => void lists.refetch()}
                />
              ) : (
                <>
                  <div
                    className="collection-picker-options"
                    aria-label={ko.customCollections}
                  >
                    {lists.data.items.map((item) => (
                      <label className="collection-picker-option" key={item.id}>
                        <input
                          type="checkbox"
                          aria-label={ko.collectionMembership(item.name)}
                          checked={saved.data.collection_ids.includes(item.id)}
                          disabled={
                            busy || saved.isFetching || search !== checkedSearch
                          }
                          onChange={(event) =>
                            changeMembership(item.id, event.target.checked)
                          }
                        />
                        <span>
                          {item.name}
                          <small>{ko.count(item.artwork_count)}</small>
                        </span>
                      </label>
                    ))}
                    {lists.data.total === 0 && (
                      <p className="field-help">
                        {checkedSearch
                          ? ko.noMatchingCollections
                          : ko.noCollections}
                      </p>
                    )}
                  </div>
                  {lists.data.total > lists.data.page_size && (
                    <div className="pagination">
                      <Button
                        size="sm"
                        variant="ghost"
                        isDisabled={busy || page <= 1 || lists.isFetching}
                        onPress={() => setPage(page - 1)}
                      >
                        {ko.previous}
                      </Button>
                      <span>
                        {ko.pagePosition(
                          page,
                          Math.ceil(lists.data.total / lists.data.page_size),
                        )}
                      </span>
                      <Button
                        size="sm"
                        variant="ghost"
                        isDisabled={
                          busy ||
                          page * lists.data.page_size >= lists.data.total ||
                          lists.isFetching
                        }
                        onPress={() => setPage(page + 1)}
                      >
                        {ko.next}
                      </Button>
                    </div>
                  )}
                </>
              )}
              <form
                className="collection-create-form picker-create-form"
                onSubmit={(event) => {
                  event.preventDefault();
                  if (target && !busy && name.trim())
                    create.mutate({ name, artwork_id: target.id });
                }}
              >
                <TextField
                  value={name}
                  onChange={setName}
                  isRequired
                  isDisabled={busy}
                >
                  <Label>{ko.collectionName}</Label>
                  <Input maxLength={100} placeholder={ko.createCollection} />
                </TextField>
                <Button
                  type="submit"
                  variant="secondary"
                  isPending={create.isPending}
                  isDisabled={busy || !name.trim()}
                >
                  <Plus size={17} />
                  {ko.createCollectionAndSave}
                </Button>
              </form>
            </>
          )}
          {membership.error && <ErrorState error={membership.error} />}
          {create.error && <ErrorState error={create.error} />}
        </div>
        <div className="saved-picker-footer">
          <p className="saved-message" role="status" aria-live="polite">
            {message || ko.collectionSavedImmediately}
          </p>
          <Button
            variant="primary"
            isDisabled={busy}
            onPress={() => dialog.current?.close()}
          >
            <Check size={17} />
            {ko.collectionPickerDone}
          </Button>
        </div>
      </dialog>
    </PickerContext.Provider>
  );
}

export function ArtworkSaveActions({
  artwork,
  compact = false,
}: {
  artwork: Artwork;
  compact?: boolean;
}) {
  const open = useContext(PickerContext);
  const bookmark = useRef<HTMLButtonElement>(null);
  const count = artwork.collection_count + Number(artwork.favorite);
  return (
    <div
      className={compact ? "artwork-quick-actions" : "artwork-save-actions"}
      role="group"
      aria-label={ko.saveActions}
    >
      <Button
        ref={bookmark}
        variant="secondary"
        size="sm"
        isIconOnly={compact}
        className={count > 0 ? "save-action is-saved" : "save-action"}
        aria-label={`${ko.collectionChoose(artwork.title)}${count ? `, ${ko.savedCollectionCount(count)}` : ""}`}
        aria-haspopup="dialog"
        onPress={() => open?.(artwork, bookmark.current)}
      >
        <Bookmark size={18} fill={count ? "currentColor" : "none"} />
        {!compact && ko.chooseCollection}
      </Button>
    </div>
  );
}
