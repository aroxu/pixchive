import { Button, Input, Label, TextField } from "@heroui/react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  ArrowLeft,
  Bookmark,
  ChevronRight,
  Pencil,
  Plus,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import {
  Link,
  useLocation,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { api, body, type Models } from "../api/client";
import { favoriteCollectionId, favoriteCollectionPath } from "../api/library";
import { useSaveRefresh } from "../components/ArtworkSaveActions";
import { ErrorState, Loading, PageHeading } from "../components/Common";
import { ko } from "../i18n/ko";
import { Library } from "./Library";

function collectionDirectoryFrom(value: unknown) {
  return typeof value === "string" &&
    (value === "/collections" || value.startsWith("/collections?"))
    ? value
    : "/collections";
}

function CollectionsBackLink() {
  const location = useLocation();
  return (
    <Link
      className="text-link directory-back"
      to={collectionDirectoryFrom(location.state?.directoryFrom)}
    >
      <ArrowLeft size={16} />
      {ko.backCollections}
    </Link>
  );
}

function FavoriteCollectionPage() {
  return (
    <>
      <CollectionsBackLink />
      <PageHeading
        eyebrow={ko.favoriteSystemLabel}
        title={ko.favorites}
        description={ko.favoriteSystemHelp}
      />
      <Library scope={{ favorite: "true" }} hideHeading />
    </>
  );
}

function CreateCollectionDialog({
  onOpen,
  onCreated,
}: {
  onOpen: () => void;
  onCreated: () => void;
}) {
  const [name, setName] = useState("");
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const helpId = `${titleId}-help`;
  const refresh = useSaveRefresh();
  const create = useMutation({
    mutationFn: () =>
      api<Models["SavedCollectionView"]>("/collections", {
        method: "POST",
        body: body({ name }),
      }),
    onSuccess: async () => {
      await refresh();
      onCreated();
      dialog.current?.close();
    },
  });
  const open = () => {
    if (create.isPending) return;
    setName("");
    create.reset();
    onOpen();
    dialog.current?.showModal();
  };
  return (
    <>
      <Button
        ref={trigger}
        className="collection-create-fab"
        variant="primary"
        isIconOnly
        aria-label={ko.createCollection}
        aria-haspopup="dialog"
        onPress={open}
      >
        <Plus size={24} aria-hidden="true" />
      </Button>
      <dialog
        ref={dialog}
        className="saved-collection-dialog create-collection-dialog"
        aria-labelledby={titleId}
        aria-describedby={helpId}
        onCancel={(event) => {
          if (create.isPending) event.preventDefault();
        }}
        onClose={() => {
          setName("");
          create.reset();
          trigger.current?.focus();
        }}
      >
        <div className="sheet-handle" aria-hidden="true" />
        <div className="dialog-heading">
          <h2 id={titleId}>{ko.createCollection}</h2>
          <Button
            type="button"
            variant="ghost"
            isIconOnly
            aria-label={ko.close}
            isDisabled={create.isPending}
            onPress={() => dialog.current?.close()}
          >
            <X size={20} aria-hidden="true" />
          </Button>
        </div>
        <form
          className="collection-dialog-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (!create.isPending && name.trim()) create.mutate();
          }}
        >
          <div className="saved-picker-body">
            <p id={helpId} className="section-description">
              {ko.createCollectionHelp}
            </p>
            <TextField
              value={name}
              onChange={(value) => {
                setName(value);
                create.reset();
              }}
              isRequired
              isDisabled={create.isPending}
            >
              <Label>{ko.collectionName}</Label>
              <Input maxLength={100} autoFocus />
            </TextField>
            {create.error && <ErrorState error={create.error} />}
          </div>
          <div className="saved-picker-footer">
            <Button
              type="button"
              variant="ghost"
              isDisabled={create.isPending}
              onPress={() => dialog.current?.close()}
            >
              {ko.cancel}
            </Button>
            <Button
              type="submit"
              variant="primary"
              isPending={create.isPending}
              isDisabled={create.isPending || !name.trim()}
            >
              {ko.createCollection}
            </Button>
          </div>
        </form>
      </dialog>
    </>
  );
}

export function SavedCollections() {
  const location = useLocation();
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const page = Math.max(1, Number(params.get("page")) || 1);
  const sort = params.get("sort") === "count" ? "count" : "name";
  const [search, setSearch] = useState(q);
  const [created, setCreated] = useState(false);
  const resultsHeading = useRef<HTMLParagraphElement>(null);
  const focusResults = useRef(false);
  const directoryState = { directoryFrom: `/collections${location.search}` };
  const change = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    value ? next.set(key, value) : next.delete(key);
    if (key !== "page") next.delete("page");
    focusResults.current = true;
    setParams(next);
  };
  const query = useQuery({
    queryKey: ["saved-collections", q, page, sort],
    queryFn: () =>
      api<Models["SavedCollectionList"]>(
        `/collections?${new URLSearchParams({ q, page: String(page), sort })}`,
      ),
  });
  const favorites = useQuery({
    queryKey: ["favorite-summary"],
    queryFn: () =>
      api<Models["ArtworkList"]>("/artworks?favorite=true&page_size=1"),
  });
  useEffect(() => {
    setSearch(q);
  }, [q]);
  useEffect(() => {
    if (!query.data || query.isFetching || query.isError) return;
    const lastPage = Math.max(
      1,
      Math.ceil(query.data.total / query.data.page_size),
    );
    if (query.data.page > lastPage) {
      focusResults.current = true;
      const next = new URLSearchParams(params);
      if (lastPage === 1) next.delete("page");
      else next.set("page", String(lastPage));
      setParams(next, { replace: true });
    } else if (focusResults.current) {
      resultsHeading.current?.focus({ preventScroll: true });
      focusResults.current = false;
    }
  }, [query.data, query.isFetching, query.isError, params, setParams]);
  return (
    <>
      <PageHeading
        title={ko.savedCollections}
        description={ko.savedCollectionsDescription}
      />
      <form
        className="search-bar collection-directory-toolbar"
        onSubmit={(event) => {
          event.preventDefault();
          change("q", search);
        }}
      >
        <Input
          aria-label={ko.collectionSearch}
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          maxLength={100}
        />
        <Button type="submit" variant="secondary">
          {ko.searchButton}
        </Button>
        <label className="sort-label">
          {ko.sort}
          <select
            value={sort}
            onChange={(event) => change("sort", event.target.value)}
          >
            <option value="name">{ko.directorySortName}</option>
            <option value="count">{ko.directorySortCount}</option>
          </select>
        </label>
      </form>
      {created && (
        <p className="saved-message" role="status">
          {ko.collectionCreated}
        </p>
      )}
      <p
        ref={resultsHeading}
        className="section-description"
        role="status"
        tabIndex={-1}
      >
        {ko.savedCollections}
        {query.isSuccess && ` · ${query.data.total + 1}`}
      </p>
      <div className="saved-collections-grid">
        <Link
          className="saved-collection-card system-collection-card"
          to={favoriteCollectionPath}
          state={directoryState}
        >
          <Bookmark size={24} fill="currentColor" aria-hidden="true" />
          <span>
            <strong>{ko.favorites}</strong>
            <span className="system-collection-badge">
              {ko.favoriteSystemLabel}
            </span>
            {favorites.data ? (
              <small>{ko.count(favorites.data.total)}</small>
            ) : favorites.isPending ? (
              <small role="status">{ko.loading}</small>
            ) : null}
          </span>
          <ChevronRight size={19} aria-hidden="true" />
        </Link>
        {query.isSuccess &&
          query.data.items.map((item) => (
            <Link
              className="saved-collection-card"
              to={`/collections/${item.id}`}
              state={directoryState}
              key={item.id}
            >
              <Bookmark size={24} aria-hidden="true" />
              <span>
                <strong>{item.name}</strong>
                <small>{ko.count(item.artwork_count)}</small>
              </span>
              <ChevronRight size={19} aria-hidden="true" />
            </Link>
          ))}
      </div>
      {favorites.error && (
        <section aria-label={ko.favorites}>
          <ErrorState
            error={favorites.error}
            retry={() => void favorites.refetch()}
          />
        </section>
      )}
      {query.isPending ? (
        <Loading />
      ) : query.error ? (
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      ) : (
        <>
          {query.data.total === 0 && (
            <section className="empty-state">
              <div className="empty-icon">
                <Bookmark size={31} />
              </div>
              <h2>{q ? ko.noMatchingCollections : ko.customCollections}</h2>
              <p>{ko.noCollectionsHelp}</p>
            </section>
          )}
          {query.data.total > query.data.page_size && (
            <div className="pagination">
              <Button
                variant="secondary"
                isDisabled={page <= 1 || query.isFetching}
                onPress={() => change("page", String(page - 1))}
              >
                {ko.previous}
              </Button>
              <span>
                {ko.customCollections} ·{" "}
                {ko.pagePosition(
                  page,
                  Math.ceil(query.data.total / query.data.page_size),
                )}
              </span>
              <Button
                variant="secondary"
                isDisabled={
                  page * query.data.page_size >= query.data.total ||
                  query.isFetching
                }
                onPress={() => change("page", String(page + 1))}
              >
                {ko.next}
              </Button>
            </div>
          )}
        </>
      )}
      <CreateCollectionDialog
        onOpen={() => setCreated(false)}
        onCreated={() => {
          const next = new URLSearchParams(params);
          next.delete("q");
          next.delete("page");
          focusResults.current = false;
          setSearch("");
          setParams(next);
          setCreated(true);
        }}
      />
    </>
  );
}

function CollectionManagement({
  collection,
}: {
  collection: Models["SavedCollectionView"];
}) {
  const [name, setName] = useState(collection.name);
  const [action, setAction] = useState<"rename" | "delete">("rename");
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const renameTrigger = useRef<HTMLButtonElement>(null);
  const deleteTrigger = useRef<HTMLButtonElement>(null);
  const navigate = useNavigate();
  const location = useLocation();
  const refresh = useSaveRefresh();
  const mutation = useMutation({
    mutationFn: () =>
      api(
        `/collections/${collection.id}`,
        action === "delete"
          ? { method: "DELETE" }
          : { method: "PATCH", body: body({ name }) },
      ),
    onSuccess: async () => {
      dialog.current?.close();
      if (action === "delete")
        navigate(collectionDirectoryFrom(location.state?.directoryFrom), {
          replace: true,
        });
      await refresh();
    },
  });
  const open = (next: "rename" | "delete") => {
    trigger.current =
      next === "rename" ? renameTrigger.current : deleteTrigger.current;
    setAction(next);
    setName(collection.name);
    mutation.reset();
    dialog.current?.showModal();
  };
  return (
    <>
      <div className="form-actions">
        <Button
          ref={renameTrigger}
          size="sm"
          variant="secondary"
          onPress={() => open("rename")}
        >
          <Pencil size={16} />
          {ko.renameCollection}
        </Button>
        <Button
          ref={deleteTrigger}
          size="sm"
          variant="ghost"
          onPress={() => open("delete")}
        >
          <Trash2 size={16} />
          {ko.deleteCollection}
        </Button>
      </div>
      <dialog
        ref={dialog}
        className="confirm-dialog"
        aria-labelledby="manage-collection-title"
        onCancel={(event) => {
          if (mutation.isPending) event.preventDefault();
        }}
        onClose={() => trigger.current?.focus()}
      >
        <div className="dialog-heading">
          <h2 id="manage-collection-title">
            {action === "rename" ? ko.renameCollection : ko.deleteCollection}
          </h2>
          <Button
            variant="ghost"
            isIconOnly
            aria-label={ko.close}
            isDisabled={mutation.isPending}
            onPress={() => dialog.current?.close()}
          >
            <X size={19} />
          </Button>
        </div>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (!mutation.isPending) mutation.mutate();
          }}
        >
          {action === "delete" ? (
            <>
              <p className="picker-artwork-title">{collection.name}</p>
              <p className="section-description">{ko.deleteCollectionHelp}</p>
            </>
          ) : (
            <TextField
              value={name}
              onChange={setName}
              isRequired
              isDisabled={mutation.isPending}
            >
              <Label>{ko.collectionName}</Label>
              <Input maxLength={100} />
            </TextField>
          )}
          {mutation.error && <ErrorState error={mutation.error} />}
          <div className="dialog-actions">
            <Button
              type="button"
              variant="ghost"
              isDisabled={mutation.isPending}
              onPress={() => dialog.current?.close()}
            >
              {ko.cancel}
            </Button>
            <Button
              type="submit"
              variant={action === "delete" ? "danger" : "primary"}
              isPending={mutation.isPending}
              isDisabled={
                action === "rename" &&
                (!name.trim() || name.trim() === collection.name)
              }
            >
              {action === "delete"
                ? ko.deleteCollection
                : ko.saveCollectionName}
            </Button>
          </div>
        </form>
      </dialog>
    </>
  );
}

export function SavedCollectionPage() {
  const { id = "" } = useParams();
  return id === favoriteCollectionId ? (
    <FavoriteCollectionPage />
  ) : (
    <CustomCollectionPage key={id} id={id} />
  );
}

function CustomCollectionPage({ id }: { id: string }) {
  const query = useQuery({
    queryKey: ["saved-collection", id],
    queryFn: () => api<Models["SavedCollectionView"]>(`/collections/${id}`),
  });
  if (query.isPending) return <Loading />;
  if (query.error)
    return (
      <ErrorState error={query.error} retry={() => void query.refetch()} />
    );
  return (
    <>
      <CollectionsBackLink />
      <PageHeading
        title={query.data.name}
        description={ko.count(query.data.artwork_count)}
        action={
          <CollectionManagement key={query.data.id} collection={query.data} />
        }
      />
      <Library scope={{ collection_id: query.data.id }} hideHeading />
    </>
  );
}
