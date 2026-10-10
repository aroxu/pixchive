import { useQuery } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import { useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { api, type Models } from "../api/client";
import { ErrorState, Loading, PageHeading } from "../components/Common";
import { JobCard } from "../components/JobStatus";
import { PageNavigation } from "../components/PageNavigation";
import { ko } from "../i18n/ko";
import { formatCount, formatDateTime } from "../i18n/format";

function RelatedJobs({
  id,
  direction,
  from,
}: {
  id: string;
  direction: "parents" | "children";
  from: string;
}) {
  const [page, setPage] = useState(1);
  const query = useQuery({
    queryKey: ["job-related", id, direction, page],
    queryFn: () =>
      api<Models["JobList"]>(`/jobs/${id}/${direction}?page=${page}`),
    refetchInterval: 3000,
  });
  const title = direction === "parents" ? ko.parentJobs : ko.childJobs;
  return (
    <section className="job-detail-section">
      <h2>{title}</h2>
      <p className="section-description">
        {direction === "parents" ? ko.parentJobsHelp : ko.childJobsHelp}
      </p>
      {query.isPending ? (
        <Loading />
      ) : query.error ? (
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      ) : (
        <>
          <div className="job-list">
            {query.data.items.map((job) => (
              <JobCard key={job.id} job={job} from={from} />
            ))}
          </div>
          <PageNavigation
            page={query.data.page}
            pageSize={query.data.page_size}
            total={query.data.total}
            label={title}
            onChange={setPage}
          />
        </>
      )}
    </section>
  );
}

function JobRecords({ id, kind }: { id: string; kind: "issues" | "events" }) {
  const [open, setOpen] = useState(kind === "issues");
  const [page, setPage] = useState(1);
  const issues = useQuery({
    queryKey: ["job-issues", id, page],
    queryFn: () =>
      api<Models["JobIssueList"]>(`/jobs/${id}/issues/history?page=${page}`),
    enabled: open && kind === "issues",
    refetchInterval: 3000,
  });
  const events = useQuery({
    queryKey: ["job-events", id, page],
    queryFn: () =>
      api<Models["JobEventList"]>(`/jobs/${id}/events/history?page=${page}`),
    enabled: open && kind === "events",
    refetchInterval: 5000,
  });
  const query = kind === "issues" ? issues : events;
  const title = kind === "issues" ? ko.issueDetails : ko.jobEvents;
  return (
    <details
      className="job-issues job-records"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>{title}</summary>
      {open &&
        (query.isPending ? (
          <Loading />
        ) : query.error ? (
          <ErrorState error={query.error} retry={() => void query.refetch()} />
        ) : (
          query.data && (
            <>
              <p className="section-description">
                {kind === "issues"
                  ? ko.issueHistoryHelp
                  : ko.jobEventHistoryHelp}
              </p>
              {query.data.total === 0 && (
                <p className="section-description">
                  {kind === "issues" ? ko.noJobIssues : ko.noJobEvents}
                </p>
              )}
              <ul>
                {kind === "issues"
                  ? issues.data?.items.map((item, index) => (
                      <li key={`${page}-${index}`}>
                        <Link
                          className="inline-link"
                          to={`/artworks/${item.artwork_id}`}
                        >
                          #{item.artwork_id}
                          {item.page !== null &&
                            ` · ${ko.pages(item.page + 1)}`}
                        </Link>
                        <span>
                          {ko.jobErrors[item.code] ?? ko.requestFailed}
                        </span>
                        {item.role && (
                          <small>
                            {ko.roles[item.role] ?? ko.unknownState}
                          </small>
                        )}
                      </li>
                    ))
                  : events.data?.items.map((item, index) => (
                      <li key={`${page}-${index}`}>
                        <strong>
                          {formatDateTime(item.created_at)} ·{" "}
                          {ko.jobStates[item.state] ?? ko.unknownState}
                        </strong>
                        {item.code && (
                          <span>
                            {ko.jobErrors[item.code] ?? ko.requestFailed}
                          </span>
                        )}
                      </li>
                    ))}
              </ul>
              <PageNavigation
                page={query.data.page}
                pageSize={query.data.page_size}
                total={query.data.total}
                label={title}
                onChange={setPage}
              />
            </>
          )
        ))}
    </details>
  );
}

export function JobDetail() {
  const { id = "" } = useParams();
  const location = useLocation();
  const previous = location.state?.from;
  const from =
    typeof previous === "string" && /^\/jobs(?:\?.*)?$/.test(previous)
      ? previous
      : "/jobs";
  const query = useQuery({
    queryKey: ["job", id],
    queryFn: () => api<Models["JobDetail"]>(`/jobs/${id}`),
    refetchInterval: 3000,
  });
  return (
    <>
      <Link className="back-link" to={from}>
        <ArrowLeft size={18} />
        {ko.backToJobs}
      </Link>
      <PageHeading title={ko.jobDetail} description={ko.jobDetailHelp} />
      {query.isPending ? (
        <Loading />
      ) : query.error ? (
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      ) : (
        <>
          <JobCard key={id} job={query.data} from={from} detailed />
          {query.data.downloads && (
            <section className="job-detail-section">
              <h2>{ko.downloadResults}</h2>
              <p className="section-description">{ko.downloadResultsHelp}</p>
              <dl className="status-list">
                {Object.entries(query.data.downloads).map(([state, count]) => (
                  <div key={state}>
                    <dt>{ko.downloadStates[state]}</dt>
                    <dd>{formatCount(count)}</dd>
                  </div>
                ))}
              </dl>
            </section>
          )}
          {query.data.parent_count > 0 && (
            <RelatedJobs
              key={`${id}-parents`}
              id={id}
              direction="parents"
              from={from}
            />
          )}
          {query.data.child_count > 0 && (
            <RelatedJobs
              key={`${id}-children`}
              id={id}
              direction="children"
              from={from}
            />
          )}
          {["sync", "restore", "repair", "integrity", "preview"].includes(
            query.data.kind,
          ) && <JobRecords key={`${id}-issues`} id={id} kind="issues" />}
          <JobRecords key={`${id}-events`} id={id} kind="events" />
        </>
      )}
    </>
  );
}
