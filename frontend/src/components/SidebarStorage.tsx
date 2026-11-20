import { Button } from "@heroui/react";
import { useQuery } from "@tanstack/react-query";
import { ArrowUpRight, HardDrive } from "lucide-react";
import { Link } from "react-router-dom";
import { api, type Models } from "../api/client";
import { useCompactLayout } from "../hooks/useCompactLayout";
import { formatBytes } from "../i18n/format";
import { ko } from "../i18n/ko";

function storageUsage(data: Models["SystemStatus"] | undefined) {
  if (
    !data ||
    !Number.isFinite(data.storage_total) ||
    data.storage_total <= 0 ||
    !Number.isFinite(data.storage_free) ||
    data.storage_free < 0 ||
    data.storage_free > data.storage_total ||
    !Array.isArray(data.archive) ||
    data.archive.some((item) => !Number.isFinite(item.bytes) || item.bytes < 0)
  ) {
    return null;
  }
  const archive = data.archive.reduce((sum, item) => sum + item.bytes, 0);
  if (!Number.isFinite(archive)) return null;
  return {
    total: data.storage_total,
    free: data.storage_free,
    used: data.storage_total - data.storage_free,
    archive,
    low: data.storage_low,
  };
}

export function SidebarStorage() {
  const compact = useCompactLayout();
  const status = useQuery({
    queryKey: ["system"],
    queryFn: () => api<Models["SystemStatus"]>("/system/status"),
    enabled: !compact,
    refetchInterval: 30_000,
    staleTime: 30_000,
  });
  const usage = storageUsage(status.data);
  const failed = status.isError;
  const loading = status.isPending && !status.data;
  const usageText = usage
    ? ko.sidebarStorageUsage(formatBytes(usage.used), formatBytes(usage.total))
    : undefined;

  return (
    <section
      className="sidebar-storage"
      aria-label={ko.sidebarStorageStatus}
      aria-busy={status.isFetching}
      data-low={!failed && usage?.low ? true : undefined}
    >
      <Link
        className="sidebar-storage-heading"
        to="/settings"
        aria-label={ko.sidebarStorageDetails}
      >
        <HardDrive size={17} aria-hidden="true" />
        <span>{ko.storage}</span>
        <ArrowUpRight size={14} aria-hidden="true" />
      </Link>
      {loading ? (
        <p className="sidebar-storage-feedback" role="status">
          {ko.loading}
        </p>
      ) : failed || !usage ? (
        <>
          <p className="sidebar-storage-feedback" role="status">
            {failed && status.data
              ? ko.sidebarStorageStale
              : ko.sidebarStorageUnavailable}
          </p>
          <Button
            className="sidebar-storage-retry"
            size="sm"
            variant="ghost"
            isDisabled={status.isFetching}
            onPress={() => void status.refetch()}
          >
            {ko.retry}
          </Button>
        </>
      ) : (
        <>
          <p className="sidebar-storage-used" aria-label={usageText}>
            <strong>{formatBytes(usage.used)}</strong>
            <span> / {formatBytes(usage.total)}</span>
          </p>
          <progress
            className="sidebar-storage-meter"
            max={usage.total}
            value={usage.used}
            aria-label={ko.sidebarStorageUsed}
            aria-valuemin={0}
            aria-valuemax={usage.total}
            aria-valuenow={usage.used}
            aria-valuetext={usageText}
          />
          <dl className="sidebar-storage-values">
            <div>
              <dt>{ko.sidebarStorageFree}</dt>
              <dd>{formatBytes(usage.free)}</dd>
            </div>
            <div>
              <dt>{ko.sidebarStorageArchive}</dt>
              <dd>{formatBytes(usage.archive)}</dd>
            </div>
          </dl>
          {usage.low && (
            <p className="sidebar-storage-feedback" role="status">
              {ko.sidebarStorageLow}
            </p>
          )}
        </>
      )}
    </section>
  );
}
