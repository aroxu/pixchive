import { Button, Input, Label, TextField } from "@heroui/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, KeyRound, Link2, RefreshCw } from "lucide-react";
import { useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { api, body, newIdempotencyKey, type Models } from "../api/client";
import { ErrorState, Loading } from "./Common";
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
  const [cookie, setCookie] = useState("");
  const [scope, setScope] = useState("all");
  const [mode, setMode] = useState("normal");
  const requestedAt = useRef(0);
  const client = useQueryClient();
  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: () => api<Models["Account"][]>("/accounts"),
    refetchInterval: 3000,
  });
  const validate = useMutation({
    mutationFn: () =>
      api<Models["JobView"]>("/accounts/1/validate", {
        method: "POST",
        headers: { "Idempotency-Key": newIdempotencyKey() },
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["jobs"] });
    },
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
  const save = useMutation({
    mutationFn: () =>
      api("/accounts/1/credential", {
        method: "PUT",
        body: body({ phpsessid: cookie }),
      }),
    onSuccess: () => {
      setCookie("");
      validate.reset();
      sync.reset();
      void client.invalidateQueries({ queryKey: ["accounts"] });
    },
  });
  const disconnect = useMutation({
    mutationFn: () => api("/accounts/1/credential", { method: "DELETE" }),
    onSuccess: () => {
      save.reset();
      validate.reset();
      sync.reset();
      void client.invalidateQueries({ queryKey: ["accounts"] });
    },
  });
  return (
    <>
      <section className="settings-card">
        <div className="section-title">
          <Link2 size={20} />
          <h2>{onboarding ? ko.setupConnection : ko.pixivConnection}</h2>
        </div>
        {accounts.isPending ? (
          <Loading />
        ) : accounts.error ? (
          <ErrorState
            error={accounts.error}
            retry={() => void accounts.refetch()}
          />
        ) : (
          <span className="connection-state">
            <span
              className={`status-dot ${accounts.data[0]?.status === "connected" ? "" : "muted"}`}
            />
            {accounts.data[0]?.credential_present
              ? (ko.connectionStates[accounts.data[0].status] ??
                ko.unknownState)
              : ko.disconnected}
          </span>
        )}
        {accounts.data?.[0]?.validated_at && (
          <p className="section-description">
            {ko.validatedAt}:{" "}
            {new Date(accounts.data[0].validated_at * 1000).toLocaleString(
              "ko-KR",
            )}
            {accounts.data[0].pixiv_id && ` · ID ${accounts.data[0].pixiv_id}`}
          </p>
        )}
        <p className="notice">
          {accounts.data?.[0]?.credential_kind === "refresh_token"
            ? ko.cookieMigration
            : ko.integrationPending}
        </p>
        {!onboarding && <p className="section-description">{ko.supported}</p>}
        <form
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate();
          }}
        >
          <TextField value={cookie} onChange={setCookie} isRequired>
            <Label>
              <KeyRound size={14} />
              {ko.sessionCookie}
            </Label>
            <Input
              type="password"
              autoComplete="off"
              minLength={16}
              spellCheck={false}
              autoCapitalize="none"
              maxLength={4096}
            />
            <small>{ko.cookieHelp}</small>
          </TextField>
          <details className="cookie-help">
            <summary>{ko.cookieInstructions}</summary>
            <ol>
              {ko.cookieSteps.map((step) => (
                <li key={step}>{step}</li>
              ))}
            </ol>
            <p>{ko.cookieExpiry}</p>
          </details>
          <div className="form-actions">
            <Button type="submit" isPending={save.isPending}>
              {ko.saveCookie}
            </Button>
            {accounts.data?.[0]?.credential_present && (
              <Button
                variant="secondary"
                isPending={validate.isPending}
                isDisabled={
                  accounts.data?.[0]?.credential_kind !== "pixiv_cookie"
                }
                onPress={() => validate.mutate()}
              >
                {ko.validateConnection}
              </Button>
            )}
            {accounts.data?.[0]?.credential_present && (
              <Button
                variant="ghost"
                isPending={disconnect.isPending}
                onPress={() => disconnect.mutate()}
              >
                {ko.disconnect}
              </Button>
            )}
          </div>
        </form>
        {save.isSuccess && (
          <p className="success-message" role="status">
            <Check size={16} />
            {ko.cookieSaved}
          </p>
        )}
        {save.error && <ErrorState error={save.error} />}
        {disconnect.error && <ErrorState error={disconnect.error} />}
        {validate.error && <ErrorState error={validate.error} />}
        {validate.isSuccess && (
          <p role="status">
            <Link className="text-link" to={`/jobs/${validate.data.id}`}>
              {ko.openJob}
            </Link>
          </p>
        )}
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
