import { Button } from "@heroui/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, ShieldCheck } from "lucide-react";
import { useEffect } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, newIdempotencyKey, type Models } from "../api/client";
import { ErrorState, Loading, PageHeading } from "../components/Common";
import { JobCard } from "../components/JobStatus";
import { PageNavigation } from "../components/PageNavigation";
import { ko } from "../i18n/ko";
import { formatCount } from "../i18n/format";

export function Jobs() {
  const client = useQueryClient();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const kind = Object.hasOwn(ko.jobKinds, params.get("kind") ?? "")
    ? params.get("kind")!
    : "";
  const state = Object.hasOwn(ko.jobStates, params.get("state") ?? "")
    ? params.get("state")!
    : "";
  const origin = ["manual", "schedule"].includes(params.get("origin") ?? "")
    ? params.get("origin")!
    : "";
  const parsedPage = Number(params.get("page") ?? 1);
  const page =
    Number.isInteger(parsedPage) && parsedPage >= 1 && parsedPage <= 1_000_000
      ? parsedPage
      : 1;
  const search = new URLSearchParams({ page: String(page), page_size: "20" });
  for (const [key, value] of Object.entries({ kind, state, origin }))
    if (value) search.set(key, value);
  const query = useQuery({
    queryKey: ["jobs", "history", search.toString()],
    queryFn: () => api<Models["JobList"]>(`/jobs/history?${search}`),
    refetchInterval: 3000,
  });
  useEffect(() => {
    if (query.data && query.data.page !== page) {
      const actualPage = query.data.page;
      setParams(
        (current) => {
          const next = new URLSearchParams(current);
          next.set("page", String(actualPage));
          return next;
        },
        { replace: true },
      );
    }
  }, [query.data, page, setParams]);
  const update = (key: string, value: string) => {
    setParams((current) => {
      const next = new URLSearchParams(current);
      if (value) next.set(key, value);
      else next.delete(key);
      if (key !== "page") next.delete("page");
      return next;
    });
  };
  const from = `/jobs${params.size ? `?${params}` : ""}`;
  const create = useMutation({
    mutationFn: () =>
      api<Models["JobView"]>("/jobs/integrity", {
        method: "POST",
        headers: { "Idempotency-Key": newIdempotencyKey() },
      }),
    onSuccess: (job) => {
      void client.invalidateQueries({ queryKey: ["jobs"] });
      navigate(`/jobs/${job.id}`, { state: { from } });
    },
  });
  const filters = [
    { key: "kind", label: ko.jobKindFilter, value: kind, options: ko.jobKinds },
    {
      key: "state",
      label: ko.jobStateFilter,
      value: state,
      options: ko.jobStates,
    },
    {
      key: "origin",
      label: ko.jobOriginFilter,
      value: origin,
      options: { manual: ko.manualJob, schedule: ko.jobOrigins.schedule },
    },
  ];
  return (
    <>
      <PageHeading
        title={ko.jobs}
        description={ko.jobDescription}
        action={
          <Button
            variant="secondary"
            isPending={create.isPending}
            onPress={() => create.mutate()}
          >
            <ShieldCheck size={18} />
            {ko.integrity}
          </Button>
        }
      />
      <p className="section-description">{ko.integrityHelp}</p>
      <div className="job-filters">
        {filters.map((filter) => (
          <label className="sync-scope" key={filter.key}>
            <span>{filter.label}</span>
            <select
              value={filter.value}
              onChange={(event) => update(filter.key, event.target.value)}
            >
              <option value="">{ko.allJobs}</option>
              {Object.entries(filter.options).map(([value, text]) => (
                <option key={value} value={value}>
                  {text}
                </option>
              ))}
            </select>
          </label>
        ))}
        {(kind || state || origin) && (
          <Button variant="ghost" onPress={() => setParams({})}>
            {ko.clearJobFilters}
          </Button>
        )}
      </div>
      {create.error && <ErrorState error={create.error} />}
      {query.isPending ? (
        <Loading />
      ) : query.error ? (
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      ) : (
        <>
          <p className="section-description" aria-live="polite">
            {ko.jobCount(formatCount(query.data.total))}
          </p>
          {!query.data.items.length ? (
            <div className="empty-state">
              <div className="empty-icon">
                <Activity size={29} />
              </div>
              <h2>{kind || state || origin ? ko.noMatchingJobs : ko.noJobs}</h2>
              <p>
                {kind || state || origin
                  ? ko.noMatchingJobsHelp
                  : ko.workerHint}
              </p>
            </div>
          ) : (
            <div className="job-list">
              {query.data.items.map((job) => (
                <JobCard key={job.id} job={job} from={from} />
              ))}
            </div>
          )}
          <PageNavigation
            page={query.data.page}
            pageSize={query.data.page_size}
            total={query.data.total}
            label={ko.jobHistoryPages}
            onChange={(next) => update("page", String(next))}
          />
        </>
      )}
    </>
  );
}
