import { Button, Input, Label, TextField } from "@heroui/react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, body, type Models } from "../api/client";
import { ErrorState } from "./Common";
import { ko } from "../i18n/ko";

export function LocalMetadata({
  artwork,
}: {
  artwork: Models["ArtworkDetail"];
}) {
  const [note, setNote] = useState(artwork.note);
  const [alias, setAlias] = useState(artwork.artist_alias);
  const client = useQueryClient();
  const saveNote = useMutation({
    mutationFn: () =>
      api<Models["ArtworkDetail"]>(`/artworks/${artwork.id}`, {
        method: "PATCH",
        body: body({ note }),
      }),
    onSuccess: (result) => {
      client.setQueryData(["artwork", artwork.id], result);
    },
  });
  const saveAlias = useMutation({
    mutationFn: () =>
      api<Models["ArtistView"]>(`/artists/${artwork.artist_id}`, {
        method: "PATCH",
        body: body({ alias }),
      }),
    onSuccess: (result) => {
      setAlias(result.alias);
      void client.invalidateQueries({ queryKey: ["artwork"] });
      void client.invalidateQueries({ queryKey: ["artworks"] });
      void client.invalidateQueries({ queryKey: ["artists"] });
      void client.invalidateQueries({ queryKey: ["neighbors"] });
    },
  });
  return (
    <section className="local-metadata">
      <h2>{ko.localEdit}</h2>
      <p className="field-help">{ko.localEditHelp}</p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          saveAlias.mutate();
        }}
      >
        <TextField
          isDisabled={saveAlias.isPending}
          value={alias}
          onChange={(value) => {
            setAlias(value);
            saveAlias.reset();
          }}
        >
          <Label>{ko.alias}</Label>
          <Input maxLength={100} />
        </TextField>
        <Button
          type="submit"
          variant="secondary"
          isPending={saveAlias.isPending}
        >
          {ko.saveAlias}
        </Button>
        {saveAlias.error && <ErrorState error={saveAlias.error} />}
        {saveAlias.isSuccess && (
          <p role="status" className="saved-message">
            {ko.saved}
          </p>
        )}
      </form>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          saveNote.mutate();
        }}
      >
        <label className="filter-field">
          {ko.note}
          <textarea
            disabled={saveNote.isPending}
            value={note}
            maxLength={10000}
            rows={5}
            onChange={(e) => {
              setNote(e.target.value);
              saveNote.reset();
            }}
          />
        </label>
        <Button
          type="submit"
          variant="secondary"
          isPending={saveNote.isPending}
        >
          {ko.saveNote}
        </Button>
        {saveNote.error && <ErrorState error={saveNote.error} />}
        {saveNote.isSuccess && (
          <p role="status" className="saved-message">
            {ko.saved}
          </p>
        )}
      </form>
    </section>
  );
}
