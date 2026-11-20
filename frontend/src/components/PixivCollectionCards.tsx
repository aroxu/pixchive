import { Button } from "@heroui/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link2, RefreshCw } from "lucide-react";
import { useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { api, body, newIdempotencyKey, type Models } from "../api/client";
import { ErrorState } from "./Common";
import { PixivLoginBrowser } from "./PixivLoginBrowser";
import { ko } from "../i18n/ko";

export function PixivCollectionCards({
  onboarding = false,
  beforeSync,
  syncDisabled = false,
}: {
  onboarding?: boolean;
  beforeSync?: ReactNode;
  syncDisabled?: boolean;
}) {
  const [scope, setScope] = useState("all");
  const [mode, setMode] = useState("normal");
  const requestedAt = useRef(0);
  const client = useQueryClient();
  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: () => api<Models["Account"][]>("/accounts"),
    refetchInterval: 3000,
  });
  const sync = useMutation({
    mutationFn: () => {
      requestedAt.current = Math.floor(Date.now() / 1000) - 5;
      return api<Models["JobView"]>("/jobs/sync", {
        method: "POST",
        headers: { "Idempotency-Key": newIdempotencyKey() },
        body: body({ scope, mode }),
      });
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["jobs"] });
    },
  });
  const disconnect = useMutation({
    mutationFn: () => api("/accounts/1/credential", { method: "DELETE" }),
    onSuccess: () => {
      sync.reset();
      void client.invalidateQueries({ queryKey: ["accounts"] });
      void client.invalidateQueries({ queryKey: ["pixiv-browser"] });
    },
  });
  return (
    <>
      <section className="settings-card">
        <div className="section-title">
          <Link2 size={20} />
          <h2>{onboarding ? ko.setupConnection : ko.pixivConnection}</h2>
        </div>
        <PixivLoginBrowser account={accounts.data?.[0]} />
        {accounts.isError && (
          <ErrorState
            error={accounts.error}
            retry={() => void accounts.refetch()}
          />
        )}
        {accounts.data?.[0]?.credential_present && (
          <div className="form-actions">
            <Button
              variant="ghost"
              isPending={disconnect.isPending}
              onPress={() => disconnect.mutate()}
            >
              {ko.disconnect}
            </Button>
          </div>
        )}
        {!onboarding && <p className="section-description">{ko.supported}</p>}
        {disconnect.error && <ErrorState error={disconnect.error} />}
      </section>
      {beforeSync}
      <section className="settings-card">
        <div className="section-title">
          <RefreshCw size={20} />
          <h2>{onboarding ? ko.setupFirstSync : ko.startSync}</h2>
        </div>
        <p className="section-description">{ko.syncHelp}</p>
        <div className="form-actions">
          <label className="sync-scope">
            <span>{ko.syncScope}</span>
            <select value={scope} onChange={(e) => setScope(e.target.value)}>
              {Object.entries(ko.scopes).map(([value, label]) => (
                <option value={value} key={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label className="sync-scope">
            <span>{ko.syncMode}</span>
            <select value={mode} onChange={(e) => setMode(e.target.value)}>
              {Object.entries(ko.syncModes).map(([value, label]) => (
                <option value={value} key={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <Button
            isPending={sync.isPending}
            isDisabled={
              syncDisabled ||
              accounts.isPending ||
              accounts.isError ||
              accounts.data?.[0]?.status !== "connected"
            }
            onPress={() => sync.mutate()}
          >
            {ko.startSync}
          </Button>
        </div>
        <p className="field-help">{ko.syncModeHelp}</p>
        {onboarding && syncDisabled && (
          <p className="notice" role="status">
            {ko.setupPolicyPending}
          </p>
        )}
        {sync.error && <ErrorState error={sync.error} />}
        {sync.isSuccess && (
          <p role="status">
            {sync.data.created_at < requestedAt.current && (
              <span className="section-description">{ko.existingSync} </span>
            )}
            <Link className="text-link" to={`/jobs/${sync.data.id}`}>
              {ko.openJob}
            </Link>
          </p>
        )}
      </section>
    </>
  );
}
