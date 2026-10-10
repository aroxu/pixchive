import { Button } from "@heroui/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RotateCcw } from "lucide-react";
import { useEffect, useRef } from "react";
import { Link } from "react-router-dom";
import { api, newIdempotencyKey, type Models } from "../api/client";
import { ko } from "../i18n/ko";
import { ErrorState, Loading } from "./Common";

const TERMINAL = new Set(["succeeded", "partial", "failed", "cancelled"]);

export function PreviewRegeneration({
  artwork,
  active,
}: {
  artwork: Models["ArtworkDetail"];
  active: boolean;
}) {
  const client = useQueryClient();
  const refreshed = useRef<string | null>(null);
  const regenerate = useMutation({
    mutationFn: (id: string) =>
      api<Models["JobView"]>(`/artworks/${id}/preview`, {
        method: "POST",
        headers: { "Idempotency-Key": newIdempotencyKey() },
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["jobs"] });
    },
  });
  const id = regenerate.data?.id;
  const job = useQuery({
    queryKey: ["job", id],
    queryFn: () => api<Models["JobDetail"]>(`/jobs/${id}`),
    enabled: active && Boolean(id),
    refetchInterval: (query) =>
      active && !TERMINAL.has(query.state.data?.state ?? "") ? 3000 : false,
  });
  useEffect(() => {
    const result = job.data;
    if (!result || !TERMINAL.has(result.state)) return;
    const completed = `${result.id}:${result.updated_at}:${result.state}`;
    if (refreshed.current === completed) return;
    refreshed.current = completed;
    for (const key of ["artworks", "artists", "neighbors", "system", "jobs"])
      void client.invalidateQueries({ queryKey: [key] });
    void client.invalidateQueries({ queryKey: ["artwork", result.artwork_id] });
    void client.invalidateQueries({
      queryKey: ["artwork-files", result.artwork_id],
    });
  }, [job.data, client]);
  const hasSource = artwork.pages.some((page) => page.index === 0);
  const stoppingOrRunning = [
    "queued",
    "running",
    "pausing",
    "cancelling",
  ].includes(job.data?.state ?? regenerate.data?.state ?? "");
  return (
    <div>
      <Button
        variant="secondary"
        isPending={regenerate.isPending}
        isDisabled={!hasSource || stoppingOrRunning}
        onPress={() => {
          if (regenerate.isPending) return;
          regenerate.mutate(artwork.id);
        }}
      >
        <RotateCcw size={15} />
        {ko.regeneratePreview}
      </Button>
      <p className="field-help">{ko.regeneratePreviewHelp}</p>
      {!hasSource && <p className="field-help">{ko.previewSourceMissing}</p>}
      {regenerate.error && <ErrorState error={regenerate.error} />}
      {regenerate.data && (
        <>
          {job.isPending && <Loading />}
          {job.error && (
            <ErrorState error={job.error} retry={() => void job.refetch()} />
          )}
          <p className="saved-message" role="status">
            {job.data
              ? ko.previewJobStatus(
                  ko.jobStates[job.data.state] ?? ko.unknownState,
                )
              : ko.previewQueued}{" "}
            <Link className="text-link" to={`/jobs/${id}`}>
              {ko.openJob}
            </Link>
          </p>
          {job.data?.error_code && (
            <p className="status-warning" role="status">
              {ko.jobErrors[job.data.error_code] ?? ko.requestFailed}
            </p>
          )}
        </>
      )}
    </div>
  );
}
