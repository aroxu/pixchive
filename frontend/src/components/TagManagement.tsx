import { Button, Input, Label, TextField } from "@heroui/react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Pencil, Plus, X } from "lucide-react";
import { useRef, useState } from "react";
import { api, body, type Models } from "../api/client";
import { ko } from "../i18n/ko";
import { useDebouncedValue } from "../hooks/useDebouncedValue";
import { useTagRefresh } from "../hooks/useArtworkTagEdit";
import { ErrorState } from "./Common";

export function TagCreateForm() {
  const [name, setName] = useState("");
  const refresh = useTagRefresh();
  const create = useMutation({
    mutationFn: () =>
      api<Models["TagView"]>("/tags", {
        method: "POST",
        body: body({ name }),
      }),
    onSuccess: () => {
      setName("");
      refresh();
    },
  });
  return (
    <section className="tag-create">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate();
        }}
      >
        <TextField
          value={name}
          isRequired
          isDisabled={create.isPending}
          onChange={(value) => {
            setName(value);
            create.reset();
          }}
        >
          <Label>{ko.createLocalTag}</Label>
          <Input maxLength={100} placeholder={ko.tagName} />
        </TextField>
        <Button type="submit" variant="secondary" isPending={create.isPending}>
          <Plus size={16} />
          {ko.createTag}
        </Button>
      </form>
      <p className="field-help">{ko.createTagHelp}</p>
      {create.error && <ErrorState error={create.error} />}
      {create.isSuccess && (
        <p className="saved-message" role="status">
          {ko.tagCreated}
        </p>
      )}
    </section>
  );
}

export function TagRenameButton({
  tag,
  onRenamed,
  focusTargetId = "tag-catalog-search",
}: {
  tag: Models["TagView"];
  onRenamed?: (result: Models["TagRenameResult"]) => void;
  focusTargetId?: string;
}) {
  const [name, setName] = useState(tag.name);
  const [open, setOpen] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const renamed = useRef(false);
  const refresh = useTagRefresh();
  const rename = useMutation({
    mutationFn: () =>
      api<Models["TagRenameResult"]>(`/tags/${tag.id}`, {
        method: "PATCH",
        body: body({ name }),
      }),
    onSuccess: (result) => {
      renamed.current = true;
      dialog.current?.close();
      document.getElementById(focusTargetId)?.focus();
      refresh();
      onRenamed?.(result);
    },
  });
  const normalized = name.trim().normalize("NFC");
  const checkedName = useDebouncedValue(normalized);
  const changed = Boolean(normalized) && normalized !== tag.name;
  const checking = normalized !== checkedName;
  const preview = useQuery({
    queryKey: ["tag-rename-preview", tag.id, checkedName],
    queryFn: ({ signal }) =>
      api<Models["TagRenamePreview"]>(
        `/tags/${tag.id}/rename-preview?${new URLSearchParams({ name: checkedName })}`,
        { signal },
      ),
    enabled: open && changed && !checking,
    staleTime: 0,
  });
  const ready =
    changed &&
    !checking &&
    !preview.isFetching &&
    !preview.error &&
    preview.data?.name === normalized;
  const existing = ready && preview.data?.merge;
  return (
    <>
      <Button
        ref={trigger}
        size="sm"
        variant="ghost"
        isIconOnly
        aria-label={ko.renameTagLabel(tag.name)}
        onPress={() => {
          renamed.current = false;
          setName(tag.name);
          rename.reset();
          setOpen(true);
          dialog.current?.showModal();
        }}
      >
        <Pencil size={16} />
      </Button>
      <dialog
        ref={dialog}
        className="confirm-dialog"
        aria-labelledby={`tag-rename-title-${tag.id}`}
        onClose={() => {
          setOpen(false);
          if (!renamed.current) trigger.current?.focus();
        }}
      >
        <div className="dialog-heading">
          <h2 id={`tag-rename-title-${tag.id}`}>{ko.renameLocalTag}</h2>
          <Button
            variant="ghost"
            isIconOnly
            aria-label={ko.close}
            onPress={() => dialog.current?.close()}
          >
            <X size={20} />
          </Button>
        </div>
        <p className="section-description">{ko.renameTagHelp}</p>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (ready && !rename.isPending) rename.mutate();
          }}
        >
          <TextField
            value={name}
            isRequired
            isDisabled={rename.isPending}
            onChange={(value) => {
              setName(value);
              rename.reset();
            }}
          >
            <Label>{ko.tagName}</Label>
            <Input maxLength={100} autoFocus />
          </TextField>
          {existing && (
            <p className="notice" role="status">
              {ko.mergeTagHelp(normalized)}
            </p>
          )}
          {changed && (checking || preview.isPending || preview.isFetching) && (
            <p className="field-help" role="status">
              {ko.checkingTagName}
            </p>
          )}
          {changed && !checking && preview.error && (
            <ErrorState
              error={preview.error}
              retry={() => void preview.refetch()}
            />
          )}
          {rename.error && <ErrorState error={rename.error} />}
          <div className="form-actions dialog-actions">
            <Button variant="secondary" onPress={() => dialog.current?.close()}>
              {ko.cancel}
            </Button>
            <Button
              type="submit"
              isPending={rename.isPending}
              isDisabled={!ready || rename.isPending}
            >
              {existing ? ko.mergeTag : ko.saveTagName}
            </Button>
          </div>
        </form>
      </dialog>
    </>
  );
}
