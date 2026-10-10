import { Button } from "@heroui/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Archive,
  CalendarClock,
  Download,
  HardDrive,
  RotateCcw,
  Shield,
  Undo2,
  Film,
} from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, body, newIdempotencyKey, type Models } from "../api/client";
import { ErrorState, Loading } from "./Common";
import { ko } from "../i18n/ko";
import { formatBytes, formatCount, formatDateTime } from "../i18n/format";

type Rating = Models["SettingsView"]["download_ratings"][number];
const DOWNLOAD_RATINGS: Rating[] = ["safe", "r18", "r18g", "unknown"];

export function DownloadPolicyCard({
  title = ko.downloadPolicy,
  onReadinessChange,
}: {
  title?: string;
  onReadinessChange?: (ready: boolean) => void;
}) {
  const client = useQueryClient();
  const [draft, setDraft] = useState<Rating[] | null>(null);
  const query = useQuery({
    queryKey: ["settings"],
    queryFn: () => api<Models["SettingsView"]>("/settings"),
  });
  const save = useMutation({
    mutationFn: (ratings: Rating[]) =>
      api<Models["SettingsView"]>("/settings", {
        method: "PATCH",
        body: body({ download_ratings: ratings }),
      }),
    onSuccess: (value) => {
      client.setQueryData(["settings"], value);
      setDraft(null);
      void client.invalidateQueries({ queryKey: ["artwork"] });
    },
  });
  const selected = draft ?? query.data?.download_ratings ?? [];
  useEffect(() => {
    onReadinessChange?.(query.isSuccess && draft === null && !save.isPending);
  }, [onReadinessChange, query.isSuccess, draft, save.isPending]);
  return (
    <section className="settings-card">
      <div className="section-title">
        <Download size={20} />
        <h2>{title}</h2>
      </div>
      <p className="section-description">{ko.downloadPolicyHelp}</p>
      {query.isPending ? (
        <Loading />
      ) : query.error ? (
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      ) : (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            onReadinessChange?.(false);
            save.mutate(selected);
          }}
        >
          <fieldset className="download-ratings" disabled={save.isPending}>
            <legend className="sr-only">{ko.downloadPolicy}</legend>
            {DOWNLOAD_RATINGS.map((rating) => (
              <label className="rating-option" key={rating}>
                <input
                  type="checkbox"
                  checked={selected.includes(rating)}
                  onChange={(event) => {
                    onReadinessChange?.(false);
                    save.reset();
                    setDraft(
                      event.target.checked
                        ? [...selected, rating]
                        : selected.filter((value) => value !== rating),
                    );
                  }}
                />
                <span>
                  {ko.ratings[rating]}
                  {rating === "unknown" && (
                    <small>{ko.unknownRatingHelp}</small>
                  )}
                </span>
              </label>
            ))}
          </fieldset>
          {!selected.length && (
            <p className="notice" role="status">
              {ko.metadataOnly}
            </p>
          )}
          <div className="form-actions">
            <Button
              type="submit"
              variant="secondary"
              isPending={save.isPending}
              isDisabled={draft === null}
            >
              {ko.saveDownloadPolicy}
            </Button>
          </div>
        </form>
      )}
      {save.error && <ErrorState error={save.error} />}
      {save.isSuccess && (
        <p className="saved-message" role="status">
          {ko.saved}
        </p>
      )}
    </section>
  );
}

type ScheduleDraft = Partial<
  Pick<
    Models["ScheduleView"],
    | "enabled"
    | "interval_hours"
    | "scope"
    | "full_refresh_days"
    | "metadata_refresh_days"
    | "request_interval_ms"
  >
>;

function Choice({
  label,
  help,
  value,
  options,
  onChange,
}: {
  label: string;
  help?: string;
  value: string | number;
  options: [string | number, string][];
  onChange: (value: string) => void;
}) {
  return (
    <label className="sync-scope">
      <span>{label}</span>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map(([option, text]) => (
          <option key={option} value={option}>
            {text}
          </option>
        ))}
      </select>
      {help && <small>{help}</small>}
    </label>
  );
}

export function ConversionCard() {
  const client = useQueryClient();
  const [draft, setDraft] = useState<number | null>(null);
  const query = useQuery({
    queryKey: ["settings"],
    queryFn: () => api<Models["SettingsView"]>("/settings"),
  });
  const save = useMutation({
    mutationFn: (value: number) =>
      api<Models["SettingsView"]>("/settings", {
        method: "PATCH",
        body: body({ conversion_concurrency: value }),
      }),
    onSuccess: (value) => {
      client.setQueryData(["settings"], value);
      setDraft(null);
    },
  });
  return (
    <section className="settings-card">
      <div className="section-title">
        <Film size={20} />
        <h2>{ko.conversionSettings}</h2>
      </div>
      <p className="field-help">{ko.conversionFormatHelp}</p>
      {query.isPending ? (
        <Loading />
      ) : query.error ? (
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      ) : (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            save.mutate(draft ?? query.data.conversion_concurrency);
          }}
        >
          <Choice
            label={ko.conversionConcurrency}
            help={ko.conversionConcurrencyHelp}
            value={draft ?? query.data.conversion_concurrency}
            options={[1, 2].map((value) => [
              value,
              ko.concurrentConversions(value),
            ])}
            onChange={(value) => {
              save.reset();
              setDraft(Number(value));
            }}
          />
          <div className="form-actions">
            <Button
              type="submit"
              variant="secondary"
              isPending={save.isPending}
              isDisabled={draft === null}
            >
              {ko.saveConversionSettings}
            </Button>
          </div>
        </form>
      )}
      {save.error && <ErrorState error={save.error} />}
      {save.isSuccess && (
        <p className="saved-message" role="status">
          {ko.saved}
        </p>
      )}
    </section>
  );
}

export function ScheduleCard({ connected }: { connected: boolean }) {
  const client = useQueryClient();
  const [draft, setDraft] = useState<ScheduleDraft>({});
  const query = useQuery({
    queryKey: ["schedule"],
    queryFn: () => api<Models["ScheduleView"]>("/schedule"),
    refetchInterval: 15_000,
  });
  const save = useMutation({
    mutationFn: (changes: ScheduleDraft) =>
      api<Models["ScheduleView"]>("/schedule", {
        method: "PATCH",
        body: body(changes),
      }),
    onSuccess: (data) => {
      client.setQueryData(["schedule"], data);
      setDraft({});
    },
  });
  if (query.isPending)
    return (
      <section className="settings-card">
        <Loading />
      </section>
    );
  if (query.error)
    return (
      <section className="settings-card">
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      </section>
    );
  const current = { ...query.data, ...draft };
  const change = (key: keyof ScheduleDraft, value: string | boolean) => {
    save.reset();
    setDraft((old) => ({
      ...old,
      [key]:
        typeof value === "boolean" || key === "scope" ? value : Number(value),
    }));
  };
  const status = query.data;
  return (
    <section className="settings-card">
      <div className="section-title">
        <CalendarClock size={20} />
        <h2>{ko.schedule}</h2>
      </div>
      <p className="section-description">{ko.scheduleHelp}</p>
      <form
        className="schedule-form"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate(draft);
        }}
      >
        <label className="toggle-row">
          <span>{ko.scheduleEnabled}</span>
          <input
            type="checkbox"
            role="switch"
            checked={current.enabled}
            onChange={(e) => change("enabled", e.target.checked)}
          />
        </label>
        <div className="schedule-grid">
          <Choice
            label={ko.scheduleInterval}
            value={current.interval_hours}
            options={[1, 3, 6, 12, 24, 48].map((n) => [n, ko.everyHours(n)])}
            onChange={(v) => change("interval_hours", v)}
          />
          <Choice
            label={ko.syncScope}
            value={current.scope}
            options={Object.entries(ko.scopes)}
            onChange={(v) => change("scope", v)}
          />
          <Choice
            label={ko.fullRefresh}
            help={ko.fullRefreshHelp}
            value={current.full_refresh_days}
            options={[
              [0, ko.off],
              ...[1, 3, 7, 14, 30].map(
                (n) => [n, ko.everyDays(n)] as [number, string],
              ),
            ]}
            onChange={(v) => change("full_refresh_days", v)}
          />
          <Choice
            label={ko.metadataRefresh}
            help={ko.metadataRefreshHelp}
            value={current.metadata_refresh_days}
            options={[7, 14, 30, 90, 180, 365].map((n) => [n, ko.afterDays(n)])}
            onChange={(v) => change("metadata_refresh_days", v)}
          />
          <Choice
            label={ko.requestInterval}
            help={ko.requestIntervalHelp}
            value={current.request_interval_ms}
            options={[500, 1000, 2000, 5000, 10000].map((n) => [
              n,
              ko.seconds(n / 1000),
            ])}
            onChange={(v) => change("request_interval_ms", v)}
          />
        </div>
        <div className="form-actions">
          <Button
            type="submit"
            variant="secondary"
            isPending={save.isPending}
            isDisabled={!Object.keys(draft).length}
          >
            {ko.saveSchedule}
          </Button>
        </div>
      </form>
      {save.error && <ErrorState error={save.error} />}
      {save.isSuccess && (
        <p className="saved-message" role="status">
          {ko.saved}
        </p>
      )}
      <dl className="status-list">
        <div>
          <dt>{ko.nextRun}</dt>
          <dd>
            {status.enabled && status.next_run_at
              ? `${formatDateTime(status.next_run_at)} · ${ko.syncModes[status.next_mode]}`
              : ko.scheduleOff}
          </dd>
        </div>
        <div>
          <dt>{ko.lastRun}</dt>
          <dd>
            {status.last_run_at ? (
              <>
                {formatDateTime(status.last_run_at)}
                {status.last_result &&
                  ` · ${ko.scheduleResults[status.last_result] ?? ko.jobErrors[status.last_result] ?? ko.unknownState}`}
                {status.last_job_id && (
                  <>
                    {" "}
                    <Link
                      className="inline-link"
                      to={`/jobs/${status.last_job_id}`}
                    >
                      {ko.openJob}
                    </Link>
                  </>
                )}
              </>
            ) : (
              ko.never
            )}
          </dd>
        </div>
        <div>
          <dt>
            {ko.lastFullSync} · {ko.scopes[status.scope]}
          </dt>
          <dd>{formatDateTime(status.last_full_sync_at) ?? ko.never}</dd>
        </div>
        {Object.entries(status.full_refreshes).map(([scope, at]) => (
          <div key={scope}>
            <dt>
              {ko.scopeFullRefresh(ko.scopes[scope as "public" | "private"])}
            </dt>
            <dd>{formatDateTime(at) ?? ko.never}</dd>
          </div>
        ))}
      </dl>
      <p className="field-help">{ko.fullRefreshEvidenceHelp}</p>
      {status.enabled && !connected && (
        <p className="notice">{ko.scheduleNeedsConnection}</p>
      )}
    </section>
  );
}

const PAGE_SIZE = 10;

export function ExclusionsCard() {
  const client = useQueryClient();
  const [page, setPage] = useState(1);
  const query = useQuery({
    queryKey: ["exclusions", page],
    queryFn: () =>
      api<Models["ExclusionList"]>(
        `/exclusions?page=${page}&page_size=${PAGE_SIZE}`,
      ),
    refetchInterval: (q) =>
      q.state.data?.items.some((item) => item.state === "deleting")
        ? 3000
        : false,
  });
  const restore = useMutation({
    mutationFn: ({ id, download }: { id: string; download: boolean }) =>
      api<Models["RestoreResult"]>(`/artworks/${id}/restore`, {
        method: "POST",
        headers: { "Idempotency-Key": newIdempotencyKey() },
        body: body({ download }),
      }),
    onSuccess: () => {
      for (const key of [
        "exclusions",
        "artworks",
        "artists",
        "tags",
        "jobs",
        "system",
      ])
        void client.invalidateQueries({ queryKey: [key] });
    },
  });
  const pages = query.data
    ? Math.max(1, Math.ceil(query.data.total / PAGE_SIZE))
    : 1;
  return (
    <section className="settings-card">
      <div className="section-title">
        <Archive size={20} />
        <h2>{ko.exclusions}</h2>
      </div>
      <p className="section-description">{ko.exclusionsHelp}</p>
      {query.isPending ? (
        <Loading />
      ) : query.error ? (
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      ) : !query.data.items.length ? (
        <p className="empty-line">{ko.noExclusions}</p>
      ) : (
        <ul className="exclusion-list">
          {query.data.items.map((item) => (
            <li key={item.artwork_id}>
              <div>
                <strong>
                  {item.hidden || !item.title
                    ? ko.hiddenExclusion(item.artwork_id)
                    : item.title}
                </strong>
                <small>
                  {[
                    item.artist_name,
                    item.state === "deleting"
                      ? ko.exclusionDeleting
                      : ko.exclusionDeleted(
                          formatDateTime(item.created_at) ?? "",
                          formatBytes(item.freed_bytes),
                        ),
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                </small>
              </div>
              <div className="exclusion-actions">
                <Button
                  size="sm"
                  variant="secondary"
                  isDisabled={item.state !== "deleted" || restore.isPending}
                  onPress={() =>
                    restore.mutate({ id: item.artwork_id, download: true })
                  }
                >
                  <RotateCcw size={14} />
                  {ko.restoreDownload}
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  isDisabled={item.state !== "deleted" || restore.isPending}
                  onPress={() =>
                    restore.mutate({ id: item.artwork_id, download: false })
                  }
                >
                  <Undo2 size={14} />
                  {ko.restoreOnly}
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}
      {query.data && query.data.total > PAGE_SIZE && (
        <div className="pagination compact">
          <Button
            size="sm"
            variant="secondary"
            isDisabled={page <= 1}
            onPress={() => setPage((p) => p - 1)}
          >
            {ko.previous}
          </Button>
          <span>{ko.pagePosition(page, pages)}</span>
          <Button
            size="sm"
            variant="secondary"
            isDisabled={page >= pages}
            onPress={() => setPage((p) => p + 1)}
          >
            {ko.next}
          </Button>
        </div>
      )}
      {restore.error && <ErrorState error={restore.error} />}
      {restore.isSuccess && (
        <p className="saved-message" role="status">
          {restore.data.job ? ko.restoreQueued : ko.restored}
          {restore.data.job && (
            <>
              {" "}
              <Link className="inline-link" to={`/jobs/${restore.data.job.id}`}>
                {ko.openJob}
              </Link>
            </>
          )}
        </p>
      )}
    </section>
  );
}

export function StorageCard({ title = ko.storage }: { title?: string }) {
  const status = useQuery({
    queryKey: ["system"],
    queryFn: () => api<Models["SystemStatus"]>("/system/status"),
    refetchInterval: 10_000,
  });
  return (
    <section className="settings-card">
      <div className="section-title">
        <HardDrive size={20} />
        <h2>{title}</h2>
      </div>
      {status.isPending ? (
        <Loading />
      ) : status.error ? (
        <ErrorState error={status.error} retry={() => void status.refetch()} />
      ) : (
        <>
          <small>{ko.availableSpace}</small>
          <div className="storage-number">
            {formatBytes(status.data.storage_free)}
          </div>
          <div className="storage-bar">
            <span
              style={{
                width: `${(1 - status.data.storage_free / status.data.storage_total) * 100}%`,
              }}
            />
          </div>
          {status.data.storage_low ? (
            <p className="notice status-warning" role="status">
              {ko.storageLow(
                formatBytes(status.data.storage_reserve),
                formatBytes(status.data.storage_reserved),
              )}
            </p>
          ) : (
            <p className="section-description">
              {ko.storageReserve(formatBytes(status.data.storage_reserve))}
            </p>
          )}
          <dl className="status-list usage-list">
            <div>
              <dt>{ko.activeStorageBudget}</dt>
              <dd>
                {formatBytes(status.data.storage_reserved)}
                <p className="field-help">{ko.activeStorageBudgetHelp}</p>
              </dd>
            </div>
            <div>
              <dt>{ko.archiveUsage}</dt>
              <dd>
                {ko.archiveCounts(
                  formatCount(status.data.artwork_count),
                  formatCount(status.data.artist_count),
                )}
                {status.data.excluded_count > 0 &&
                  ` · ${ko.excludedCount(formatCount(status.data.excluded_count))}`}
              </dd>
            </div>
            {status.data.archive.map((item) => (
              <div key={item.role}>
                <dt>{ko.roles[item.role] ?? item.role}</dt>
                <dd>
                  {formatBytes(item.bytes)} · {formatCount(item.files)}
                </dd>
              </div>
            ))}
            <div>
              <dt>{ko.database}</dt>
              <dd>{formatBytes(status.data.database_bytes)}</dd>
            </div>
          </dl>
          <div className="worker-status">
            <span>{ko.worker}</span>
            <span>
              <span
                className={`status-dot ${status.data.worker_status === "online" ? "" : "muted"}`}
              />
              {ko.workerStates[status.data.worker_status]}
            </span>
          </div>
          <div className="worker-status">
            <span>{ko.queueLabel}</span>
            <span>
              {ko.queueCounts(
                status.data.queue.queued,
                status.data.queue.running,
                status.data.queue.waiting,
              )}
            </span>
          </div>
          {status.data.worker_status !== "online" && (
            <p className="section-description">{ko.workerHint}</p>
          )}
        </>
      )}
    </section>
  );
}

export function BackupCard() {
  const status = useQuery({
    queryKey: ["system"],
    queryFn: () => api<Models["SystemStatus"]>("/system/status"),
    refetchInterval: 10_000,
  });
  return (
    <section className="settings-card">
      <div className="section-title">
        <Shield size={20} />
        <h2>{ko.backup}</h2>
      </div>
      <p className="section-description">{ko.backupHelp}</p>
      <dl className="status-list">
        <div>
          <dt>{ko.lastBackup}</dt>
          <dd>{formatDateTime(status.data?.last_backup_at) ?? ko.noBackup}</dd>
        </div>
      </dl>
      <details className="cookie-help">
        <summary>{ko.backupCommands}</summary>
        <p>{ko.backupCommandHelp}</p>
        <pre className="command-block">
          <code>
            {[
              "python -m app.cli backup --output /backup/pixchive --media",
              "python -m app.cli verify-backup /backup/pixchive/pixchive-backup-…",
              "python -m app.cli restore --from /backup/pixchive/pixchive-backup-…",
            ].join("\n")}
          </code>
        </pre>
      </details>
      <a className="text-link with-icon" href="/api/export/metadata" download>
        <Download size={15} />
        {ko.exportMetadata}
      </a>
      <p className="section-description">{ko.exportMetadataHelp}</p>
    </section>
  );
}
