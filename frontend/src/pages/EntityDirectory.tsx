import { Button, Input, Label, TextField } from "@heroui/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ArrowUpRight, Hash, UserRound } from "lucide-react";
import { useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { api, body, type Models } from "../api/client";
import { directoryOrigin } from "../api/directory";
import { ErrorState, Loading, PageHeading } from "../components/Common";
import { TagRenameButton } from "../components/TagManagement";
import { TagTranslations } from "../components/TagTranslations";
import { formatDateTime } from "../i18n/format";
import { ko } from "../i18n/ko";
import { Library } from "./Library";

function AliasEditor({ artist }: { artist: Models["ArtistDetail"] }) {
  const [alias, setAlias] = useState(artist.alias);
  const client = useQueryClient();
  const save = useMutation({
    mutationFn: () =>
      api<Models["ArtistView"]>(`/artists/${artist.id}`, {
        method: "PATCH",
        body: body({ alias }),
      }),
    onSuccess: (result) => {
      setAlias(result.alias);
      for (const key of ["artists", "artworks", "artwork", "neighbors"])
        void client.invalidateQueries({ queryKey: [key] });
    },
  });
  return (
    <form
      className="entity-alias-form"
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate();
      }}
    >
      <TextField
        value={alias}
        isDisabled={save.isPending}
        onChange={(value) => {
          setAlias(value);
          save.reset();
        }}
      >
        <Label>{ko.alias}</Label>
        <Input maxLength={100} />
      </TextField>
      <Button type="submit" variant="secondary" isPending={save.isPending}>
        {ko.saveAlias}
      </Button>
      {save.error && <ErrorState error={save.error} />}
      {save.isSuccess && (
        <p className="saved-message" role="status">
          {ko.saved}
        </p>
      )}
    </form>
  );
}

export function ArtistPage() {
  const { id } = useParams();
  const location = useLocation();
  const query = useQuery({
    queryKey: ["artists", "detail", id],
    queryFn: () => api<Models["ArtistDetail"]>(`/artists/${id}`),
  });
  if (query.isPending) return <Loading />;
  if (query.error)
    return (
      <ErrorState error={query.error} retry={() => void query.refetch()} />
    );
  const artist = query.data;
  return (
    <>
      <Link
        className="entity-back text-link with-icon"
        to={directoryOrigin(location.state?.directoryFrom, "artists")}
        state={{ restoreDirectory: true }}
      >
        <ArrowLeft size={16} />
        {ko.backArtists}
      </Link>
      <PageHeading
        eyebrow={ko.artists}
        title={artist.name}
        description={ko.artistCollectionHelp}
        action={
          <a
            className="text-link with-icon"
            href={artist.profile_url}
            target="_blank"
            rel="noreferrer"
          >
            {ko.artistPixiv}
            <ArrowUpRight size={16} />
          </a>
        }
      />
      <section className="entity-profile">
        <div className="entity-identity">
          <div className="entity-avatar">
            {artist.avatar_url ? (
              <img src={artist.avatar_url} alt={ko.avatarAlt(artist.name)} />
            ) : (
              <UserRound size={32} />
            )}
          </div>
          <div>
            <strong>{artist.alias || artist.name}</strong>
            <p>
              {ko.count(artist.artwork_count)} · ID {artist.id}
            </p>
            {artist.account && (
              <p>
                {ko.artistAccount}: {artist.account}
              </p>
            )}
            {artist.previous_names.length > 0 && (
              <p>
                {ko.previousNames}: {artist.previous_names.join(" · ")}
              </p>
            )}
          </div>
        </div>
        <AliasEditor key={artist.id} artist={artist} />
        <details className="entity-name-history">
          <summary>{ko.artistNameHistory}</summary>
          <p className="field-help">{ko.artistNameHistoryHelp}</p>
          <ul>
            {artist.name_history.map((entry) => (
              <li key={entry.name}>
                <strong>
                  {entry.name}
                  {entry.name === artist.name && (
                    <small> · {ko.currentArtistName}</small>
                  )}
                </strong>
                <small>
                  {ko.firstSeen}:{" "}
                  {formatDateTime(entry.first_seen) ?? ko.unknownDate}
                </small>
                <small>
                  {ko.lastSeen}:{" "}
                  {formatDateTime(entry.last_seen) ?? ko.unknownDate}
                </small>
              </li>
            ))}
          </ul>
        </details>
      </section>
      <Library key={artist.id} scope={{ artist: artist.id }} hideHeading />
    </>
  );
}

export function TagPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const query = useQuery({
    queryKey: ["tags", "detail", id],
    queryFn: () => api<Models["TagDetail"]>(`/tags/${id}`),
  });
  if (query.isPending) return <Loading />;
  if (query.error)
    return (
      <ErrorState error={query.error} retry={() => void query.refetch()} />
    );
  const tag = query.data;
  const local: Models["TagView"] = {
    id: tag.id,
    name: tag.name,
    source: "local",
    artwork_count:
      tag.source_counts.find((source) => source.source === "local")
        ?.artwork_count ?? 0,
    translations: tag.translations,
    editable: tag.editable,
  };
  return (
    <>
      <Link
        id="tag-detail-back"
        className="entity-back text-link with-icon"
        to={directoryOrigin(location.state?.directoryFrom, "tags")}
        state={{ restoreDirectory: true }}
      >
        <ArrowLeft size={16} />
        {ko.backTags}
      </Link>
      <PageHeading
        eyebrow={ko.tags}
        title={tag.name}
        description={ko.tagCollectionHelp}
        action={
          tag.editable && (
            <TagRenameButton
              tag={local}
              focusTargetId="tag-detail-back"
              onRenamed={(result) => {
                if (result.merged)
                  navigate(`/tags/${result.tag.id}${location.search}`, {
                    replace: true,
                    state: { directoryFrom: location.state?.directoryFrom },
                  });
              }}
            />
          )
        }
      />
      <section className="entity-profile">
        <div className="entity-identity">
          <div className="entity-avatar">
            <Hash size={32} />
          </div>
          <div>
            <strong>{ko.count(tag.artwork_count)}</strong>
            <p>{ko.tagSourceCountsHelp}</p>
          </div>
        </div>
        <dl className="entity-source-counts">
          {tag.source_counts.map((source) => (
            <div key={source.source}>
              <dt>{ko.sources[source.source] ?? source.source}</dt>
              <dd>{ko.count(source.artwork_count)}</dd>
            </div>
          ))}
        </dl>
        {!tag.editable && <p className="field-help">{ko.sharedTagHelp}</p>}
        <TagTranslations translations={tag.translations} />
      </section>
      <Library key={tag.id} scope={{ tag_id: String(tag.id) }} hideHeading />
    </>
  );
}
