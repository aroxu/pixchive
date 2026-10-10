import { Button, Input, Label, TextField } from "@heroui/react";
import { Bookmark, ArrowRight, LockKeyhole } from "lucide-react";
import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api, body, type Models } from "../api/client";
import { ko } from "../i18n/ko";
import { ErrorState } from "../components/Common";

export function Auth({
  setup,
  onSuccess,
}: {
  setup: boolean;
  onSuccess: (data: Models["SessionStatus"]) => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState("");
  const mutation = useMutation({
    mutationFn: () =>
      api<Models["SessionStatus"]>(setup ? "/setup" : "/session", {
        method: "POST",
        body: body({
          username,
          password,
          ...(setup ? { setup_token: token } : {}),
        }),
      }),
    onSuccess,
  });
  return (
    <main className="auth-page">
      <section className="auth-panel">
        <div className="brand auth-brand">
          <span className="brand-mark">
            <Bookmark fill="currentColor" size={23} />
          </span>
          {ko.brand}
        </div>
        <div className="eyebrow">{ko.tagline}</div>
        <h1>{setup ? ko.setup : ko.login}</h1>
        <p>{setup ? ko.setupDescription : ko.loginDescription}</p>
        {setup && (
          <>
            <p className="notice">{ko.supported}</p>
            <p>{ko.setupNextSteps}</p>
          </>
        )}
        <form
          onSubmit={(event) => {
            event.preventDefault();
            mutation.mutate();
          }}
        >
          {setup && (
            <TextField value={token} onChange={setToken} isRequired>
              <Label>{ko.setupToken}</Label>
              <Input type="password" autoComplete="off" minLength={16} />
              <small>{ko.setupHelp}</small>
            </TextField>
          )}
          <TextField value={username} onChange={setUsername} isRequired>
            <Label>{ko.username}</Label>
            <Input autoComplete="username" maxLength={64} />
          </TextField>
          <TextField value={password} onChange={setPassword} isRequired>
            <Label>{ko.password}</Label>
            <Input
              type="password"
              autoComplete={setup ? "new-password" : "current-password"}
              minLength={12}
              maxLength={256}
            />
            {setup && <small>{ko.passwordHelp}</small>}
          </TextField>
          {mutation.error && <ErrorState error={mutation.error} />}
          <Button type="submit" isPending={mutation.isPending} fullWidth>
            {setup ? ko.createAdmin : ko.login}
            <ArrowRight size={17} />
          </Button>
        </form>
        <div className="auth-footer">
          <LockKeyhole size={14} />
          {ko.archive}
        </div>
      </section>
    </main>
  );
}
