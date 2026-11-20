import { Button, FieldError, Input, Label, TextField } from "@heroui/react";
import { Bookmark, ArrowRight, LockKeyhole } from "lucide-react";
import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api, body, type Models } from "../api/client";
import { ko } from "../i18n/ko";
import { ErrorState } from "../components/Common";

// The server counts characters (code points), so emoji count once here too.
const length = (value: string) => [...value].length;
const PASSWORD_MIN = 12;
const SETUP_TOKEN_MIN = 16;

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
            <TextField
              value={token}
              onChange={setToken}
              isRequired
              validate={(value) =>
                value && length(value) < SETUP_TOKEN_MIN
                  ? ko.setupTokenTooShort
                  : null
              }
            >
              <Label>{ko.setupToken}</Label>
              <Input type="password" autoComplete="off" maxLength={256} />
              <small>{ko.setupHelp}</small>
              <FieldError />
            </TextField>
          )}
          <TextField value={username} onChange={setUsername} isRequired>
            <Label>{ko.username}</Label>
            <Input autoComplete="username" maxLength={64} />
            <FieldError />
          </TextField>
          <TextField
            value={password}
            onChange={setPassword}
            isRequired
            validate={(value) =>
              setup && value && length(value) < PASSWORD_MIN
                ? ko.passwordTooShort
                : null
            }
          >
            <Label>{ko.password}</Label>
            <Input
              type="password"
              autoComplete={setup ? "new-password" : "current-password"}
              maxLength={256}
            />
            {setup && <small>{ko.passwordHelp}</small>}
            <FieldError />
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
