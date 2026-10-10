import { useQuery } from "@tanstack/react-query";
import { Download } from "lucide-react";
import { useState } from "react";
import { api, type Models } from "../api/client";
import { ErrorState, Loading } from "./Common";
import { PageNavigation } from "./PageNavigation";
import { ko } from "../i18n/ko";
import { formatBytes, formatCount } from "../i18n/format";

export function ArtworkFiles({
  id,
  active = true,
}: {
  id: string;
  active?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [role, setRole] = useState("");
  const [page, setPage] = useState(1);
  const search = new URLSearchParams({ page: String(page), page_size: "12" });
  if (role) search.set("role", role);
  const query = useQuery({
    queryKey: ["artwork-files", id, role, page],
    queryFn: () => api<Models["FileList"]>(`/artworks/${id}/files?${search}`),
    enabled: active && open,
    refetchInterval: active && open ? 5000 : false,
  });
  return (
    <details
      className="artwork-files"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>{ko.artworkFiles}</summary>
      {open && (
        <>
          <p className="field-help">{ko.artworkFilesHelp}</p>
          <label className="sync-scope">
            <span>{ko.fileRole}</span>
            <select
              value={role}
              onChange={(event) => {
                setRole(event.target.value);
                setPage(1);
              }}
            >
              <option value="">{ko.allFileRoles}</option>
              {Object.entries(ko.roles).map(([value, name]) => (
                <option key={value} value={value}>
                  {name}
                </option>
              ))}
            </select>
          </label>
          {query.isPending ? (
            <Loading />
          ) : query.error ? (
            <ErrorState
              error={query.error}
              retry={() => void query.refetch()}
            />
          ) : (
            <>
              <p className="section-description" aria-live="polite">
                {ko.fileCount(formatCount(query.data.total))}
              </p>
              {!query.data.items.length && (
                <p className="section-description">{ko.noArchivedFiles}</p>
              )}
              <ul className="file-records">
                {query.data.items.map((file) => (
                  <li className="file-record" key={file.id}>
                    <div className="file-record-heading">
                      <strong>
                        {ko.roles[file.role] ?? ko.unknownState} ·{" "}
                        {ko.pages(file.page + 1)}
                      </strong>
                      <span className={`job-state state-${file.state}`}>
                        {ko.fileStates[file.state] ?? ko.unknownState}
                      </span>
                    </div>
                    <p className="section-description">
                      {formatBytes(file.size)} · {file.media_type}
                      {file.width > 0 &&
                        file.height > 0 &&
                        ` · ${file.width} × ${file.height}`}
                    </p>
                    <p className="field-help">
                      {ko.fileProvenance}:{" "}
                      {file.provenance
                        ? (ko.fileProvenances[file.provenance] ??
                          ko.unknownProvenance)
                        : ko.unknownProvenance}
                    </p>
                    <details className="file-verification">
                      <summary>{ko.fileVerification}</summary>
                      <p>
                        {ko.fileRevision}{" "}
                        <code>{file.revision.slice(0, 12)}</code>
                      </p>
                      <p>
                        SHA-256 <code>{file.sha256}</code>
                      </p>
                    </details>
                    {file.url && (
                      <a
                        className="text-link with-icon"
                        href={file.url}
                        download={file.download_name}
                      >
                        <Download size={14} />
                        {ko.downloadFile}
                      </a>
                    )}
                  </li>
                ))}
              </ul>
              <PageNavigation
                page={query.data.page}
                pageSize={query.data.page_size}
                total={query.data.total}
                label={ko.artworkFiles}
                onChange={setPage}
              />
            </>
          )}
        </>
      )}
    </details>
  );
}
