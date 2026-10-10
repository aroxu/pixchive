import { Button } from "@heroui/react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Pause, Play, X, RotateCcw } from "lucide-react";
import { Link } from "react-router-dom";
import { api, body, type Models } from "../api/client";
import { ErrorState } from "./Common";
import { ko } from "../i18n/ko";
import { formatBytes, formatCount, formatDateTime } from "../i18n/format";

const SCAN_KEYS = [
  "added",
  "changed",
  "checked",
  "unchanged",
  "unavailable",
  "excluded",
  "download_skipped",
  "error",
] as const;

export function ScanSummary({ scan }: { scan: Models["ScanCounts"] }) {
  return (
    <p className="scan-summary">
      <span>
        {ko.scanLabels.discovered} {formatCount(scan.discovered)}
      </span>
      {SCAN_KEYS.filter((key) => scan[key] > 0).map((key) => (
        <span
          key={key}
          className={key === "error" ? "status-warning" : undefined}
        >
          {ko.scanLabels[key]} {formatCount(scan[key])}
        </span>
      ))}
    </p>
  );
}

export function JobActions({
  job,
  from,
}: {
  job: Models["JobView"];
  from: string;
}) {
  const client = useQueryClient();
  const action = useMutation({
    mutationFn: (command: Models["JobAction"]["action"]) =>
      api<Models["JobView"]>(`/jobs/${job.id}/actions`, {
        method: "POST",
        body: body({ action: command }),
      }),
    onSuccess: () => {
      for (const key of [
        "jobs",
        "job",
        "job-related",
        "job-events",
        "job-issues",
        "system",
      ])
        void client.invalidateQueries({ queryKey: [key] });
    },
  });
  return (
    <>
      {action.error && <ErrorState error={action.error} />}
      {action.data && action.data.id !== job.id && (
        <p className="notice" role="status">
          {ko.jobActionMerged}{" "}
          <Link
            className="inline-link"
            to={`/jobs/${action.data.id}`}
            state={{ from }}
          >
            {ko.openMergedJob}
          </Link>
        </p>
      )}
      <div className="job-actions">
        {job.kind !== "delete" &&
          ["running", "queued", "waiting_children"].includes(job.state) && (
            <Button
              size="sm"
              variant="ghost"
              isDisabled={action.isPending}
              onPress={() => action.mutate("pause")}
            >
              <Pause size={14} />
              {ko.pause}
            </Button>
          )}
        {job.state === "paused" && (
          <Button
            size="sm"
            variant="secondary"
            isDisabled={action.isPending}
            onPress={() => action.mutate("resume")}
          >
            <Play size={14} />
            {ko.resume}
          </Button>
        )}
        {["partial", "failed", "waiting_auth", "waiting_storage"].includes(
          job.state,
        ) && (
          <Button
            size="sm"
            variant="secondary"
            isDisabled={action.isPending}
            onPress={() => action.mutate("retry")}
          >
            <RotateCcw size={14} />
            {ko.retry}
          </Button>
        )}
        {job.kind !== "delete" &&
          [
            "running",
            "queued",
            "paused",
            "pausing",
            "waiting_auth",
            "waiting_storage",
            "waiting_children",
          ].includes(job.state) && (
            <Button
              size="sm"
              variant="ghost"
              isDisabled={action.isPending}
              onPress={() => action.mutate("cancel")}
            >
              <X size={14} />
              {ko.cancel}
            </Button>
          )}
      </div>
    </>
  );
}

function StorageBudget({ budget }: { budget: Models["StorageBudget"] }) {
  const required =
    budget.additional_bytes + budget.reserved_bytes + budget.reserve_bytes;
  return (
    <div className="notice" role="status">
      <dl className="status-list">
        <div>
          <dt>{ko.storageRequired}</dt>
          <dd>{formatBytes(required)}</dd>
        </div>
        <div>
          <dt>{ko.storageFreeAtCheck}</dt>
          <dd>{formatBytes(budget.free_bytes)}</dd>
        </div>
        {budget.reserved_bytes > 0 && (
          <div>
            <dt>{ko.otherStorageBudget}</dt>
            <dd>{formatBytes(budget.reserved_bytes)}</dd>
          </div>
        )}
        <div>
          <dt>{ko.storageCheckedAt}</dt>
          <dd>{formatDateTime(budget.checked_at)}</dd>
        </div>
      </dl>
      <p className="field-help">{ko.storageBudgetHelp}</p>
    </div>
  );
}

export function JobCard({
  job,
  from,
  detailed = false,
}: {
  job: Models["JobView"];
  from: string;
  detailed?: boolean;
}) {
  const title = ko.jobKinds[job.kind] ?? ko.unknownState;
  return (
    <article className="job-card">
      <div className="job-card-heading">
        <div>
          <h2>
            {detailed ? (
              title
            ) : (
              <Link
                className="job-title-link"
                to={`/jobs/${job.id}`}
                state={{ from }}
              >
                {title}
              </Link>
            )}
          </h2>
          <div className="job-tags">
            {job.scope && (
              <small>{ko.scopes[job.scope] ?? ko.unknownState}</small>
            )}
            {job.kind === "sync" && job.mode && (
              <small>{ko.jobModes[job.mode] ?? ko.unknownState}</small>
            )}
            {job.origin !== "manual" && (
              <small>{ko.jobOrigins[job.origin] ?? ko.unknownState}</small>
            )}
            {job.artwork_id &&
              (job.kind === "delete" ? (
                <small>#{job.artwork_id}</small>
              ) : (
                <Link
                  className="inline-link"
                  to={`/artworks/${job.artwork_id}`}
                >
                  #{job.artwork_id} {ko.openArtwork}
                </Link>
              ))}
          </div>
          <time dateTime={new Date(job.created_at * 1000).toISOString()}>
            {formatDateTime(job.created_at)}
          </time>
        </div>
        <span className={`job-state state-${job.state}`}>
          {ko.jobStates[job.state] ?? ko.unknownState}
        </span>
      </div>
      <div className="job-progress">
        <span>
          {job.kind === "integrity"
            ? ko.processed(job.processed)
            : job.kind === "preview"
              ? ko.previewProcessed(job.processed)
              : job.kind === "validate"
                ? ko.validationProcessed(job.processed)
                : job.kind === "delete"
                  ? ko.deleteProcessed(job.processed)
                  : ko.collectionProcessed(job.processed)}
          {job.total !== null && ` / ${formatCount(job.total)}`}
        </span>
        {job.failed > 0 && (
          <span className="status-warning">{ko.failures(job.failed)}</span>
        )}
      </div>
      {job.scan && <ScanSummary scan={job.scan} />}
      <p className="section-description">
        {ko.jobPhases[job.phase] ?? ko.unknownState}
      </p>
      {job.error_code && (
        <p role="status" className="status-warning">
          {ko.jobErrors[job.error_code] ?? ko.requestFailed}
        </p>
      )}
      {job.state === "waiting_storage" && job.storage_budget && (
        <StorageBudget budget={job.storage_budget} />
      )}
      {job.retry_at !== null && job.retry_at * 1000 > Date.now() && (
        <p className="section-description">
          {ko.retryAfter}: {formatDateTime(job.retry_at)}
        </p>
      )}
      {job.total !== null && job.total > 0 && (
        <progress aria-label={title} value={job.processed} max={job.total} />
      )}
      {!detailed && (
        <Link
          className="inline-link job-detail-link"
          to={`/jobs/${job.id}`}
          state={{ from }}
        >
          {ko.jobDetail}
        </Link>
      )}
      <JobActions job={job} from={from} />
    </article>
  );
}
