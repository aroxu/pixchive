import { Button, Spinner } from "@heroui/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Monitor, RefreshCw, X } from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import type RFB from "@novnc/novnc";
import { api, getCsrf, type Models } from "../api/client";
import { ko } from "../i18n/ko";

type BrowserStatus = Models["BrowserStatus"];

const statusKey = ["pixiv-browser"] as const;

export function PixivLoginBrowser({
  account,
}: {
  account?: Models["Account"];
}) {
  const [open, setOpen] = useState(false);
  const [viewReady, setViewReady] = useState(false);
  const [confirmationSubmitted, setConfirmationSubmitted] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const helpId = useId();
  const client = useQueryClient();
  const status = useQuery({
    queryKey: statusKey,
    queryFn: ({ signal }) =>
      api<BrowserStatus>("/accounts/1/browser", { signal }),
    refetchInterval: (query) => {
      if (query.state.data?.enabled === false) return 30_000;
      const state = query.state.data?.state;
      return open || state === "starting" || state === "reauthenticating"
        ? 3000
        : 15_000;
    },
    refetchIntervalInBackground: false,
    retry: false,
  });
  const request = useMutation({
    mutationFn: (action: "open" | "reauth" | "login" | "complete") =>
      api<BrowserStatus>(`/accounts/1/browser/${action}`, { method: "POST" }),
    retry: false,
    onMutate: () => client.cancelQueries({ queryKey: statusKey }),
    onSuccess: (result, action) => {
      client.setQueryData(statusKey, result);
      if (action === "open" || action === "login") {
        if (result.view_available) setViewReady(true);
        setConfirmationSubmitted(false);
      } else if (action === "complete") {
        setConfirmationSubmitted(
          result.state === "reauthenticating" ||
            (result.state === "connected" && !result.login_pending),
        );
      }
      void client.invalidateQueries({ queryKey: ["accounts"] });
    },
    onSettled: () => {
      void client.invalidateQueries({ queryKey: statusKey });
    },
  });
  const data = status.data;
  const state = data?.enabled === false ? "disabled" : data?.state;
  const loginPending = data?.login_pending === true;
  const working = state === "starting" || state === "reauthenticating";
  const confirming =
    (request.isPending && request.variables === "complete") ||
    (loginPending && state === "reauthenticating");
  const description = state
    ? data?.message_code === "login_not_completed"
      ? ko.browserLoginNotCompleted
      : state === "disabled" && account?.credential_present
        ? // Without the login browser this card is the only connection
          // status, so describe the stored connection itself.
          account.status === "connected"
          ? ko.browserDisabledConnected
          : ko.browserDisabledNeedsLogin
        : ko.browserStateDescriptions[state]
    : null;
  const refreshStatus = useCallback(() => {
    void client.invalidateQueries({ queryKey: statusKey });
  }, [client]);

  useEffect(() => {
    if (data) void client.invalidateQueries({ queryKey: ["accounts"] });
  }, [client, data?.state, data?.last_synced]);

  const openView = () => {
    setOpen(true);
    dialog.current?.showModal();
    if (!request.isPending) request.mutate("open");
  };
  const closeView = useCallback(() => {
    dialog.current?.close();
    setOpen(false);
    setViewReady(false);
    trigger.current?.focus({ preventScroll: true });
  }, []);
  const completeLogin = () => {
    if (!loginPending || request.isPending || working) return;
    setConfirmationSubmitted(true);
    request.mutate("complete");
  };

  useEffect(() => {
    if (open && request.isSuccess && data?.enabled && data.view_available)
      setViewReady(true);
  }, [open, request.isSuccess, data?.enabled, data?.view_available]);

  useEffect(() => {
    if (
      confirmationSubmitted &&
      !request.isPending &&
      state === "connected" &&
      data?.login_pending === false
    ) {
      setConfirmationSubmitted(false);
      closeView();
    }
  }, [
    confirmationSubmitted,
    request.isPending,
    state,
    data?.login_pending,
    closeView,
  ]);

  // Keep the live canvas and its input focus through requests and status updates.
  const canView = open && viewReady;

  return (
    <div className="pixiv-login-browser">
      <h3 className="pixiv-browser-heading">
        <Monitor size={18} aria-hidden="true" />
        {ko.browserLogin}
      </h3>
      <p className="section-description">{ko.browserLoginHelp}</p>
      {status.isPending ? (
        <div className="pixiv-browser-status" role="status">
          <Spinner size="sm" />
          {ko.browserStatusLoading}
        </div>
      ) : status.isError ? (
        <div className="error-box" role="alert">
          <p>{ko.browserStatusFailed}</p>
          <Button variant="secondary" onPress={() => void status.refetch()}>
            <RefreshCw size={16} aria-hidden="true" />
            {ko.retry}
          </Button>
        </div>
      ) : state ? (
        <div className="pixiv-browser-status" aria-live="polite" role="status">
          <span className="connection-state" data-state={state}>
            {working ? (
              <Spinner size="sm" />
            ) : (
              <span
                className={`status-dot ${state === "connected" ? "" : "muted"}`}
                aria-hidden="true"
              />
            )}
            {ko.browserStates[state]}
          </span>
          <p className="section-description">{description}</p>
        </div>
      ) : null}
      {data && (
        <>
          {data.enabled && (
            <p className="field-help">
              {loginPending
                ? ko.browserAutoReauthPaused
                : data.auto_reauth
                  ? ko.browserAutoReauth
                  : ko.browserAutoReauthOff}
            </p>
          )}
          {loginPending && !open && (
            <p className="field-help">{ko.browserPendingCloseHelp}</p>
          )}
          <dl className="pixiv-browser-times">
            <div>
              <dt>{ko.browserLastChecked}</dt>
              <dd>
                <BrowserTime value={data.last_checked} />
              </dd>
            </div>
            <div>
              <dt>{ko.browserLastSynced}</dt>
              <dd>
                <BrowserTime value={data.last_synced} />
              </dd>
            </div>
          </dl>
        </>
      )}
      {data?.enabled && (
        <div className="form-actions">
          <Button
            ref={trigger}
            variant={loginPending ? "secondary" : "primary"}
            isDisabled={request.isPending}
            onPress={openView}
          >
            <Monitor size={16} aria-hidden="true" />
            {loginPending ? ko.browserReopen : ko.browserOpen}
          </Button>
          {loginPending && !open && (
            <Button
              isPending={confirming}
              isDisabled={request.isPending || working}
              onPress={completeLogin}
            >
              {ko.browserCompleteLogin}
            </Button>
          )}
          <Button
            variant="secondary"
            isPending={request.isPending && request.variables === "reauth"}
            isDisabled={request.isPending || working || loginPending}
            onPress={() => request.mutate("reauth")}
          >
            <RefreshCw size={16} aria-hidden="true" />
            {ko.browserReauth}
          </Button>
        </div>
      )}
      {request.isError && !open && (
        <p className="error-box" role="alert">
          {ko.browserRequestFailed}
        </p>
      )}
      <dialog
        ref={dialog}
        className="pixiv-browser-dialog"
        aria-labelledby={titleId}
        aria-describedby={helpId}
        onCancel={(event) => {
          event.preventDefault();
          closeView();
        }}
        onKeyDownCapture={(event) => {
          // The remote screen captures keys, including Escape.
          if (event.key === "Escape") {
            event.preventDefault();
            event.stopPropagation();
            closeView();
          }
        }}
        onClose={() => {
          setOpen(false);
          setViewReady(false);
          trigger.current?.focus({ preventScroll: true });
        }}
      >
        <div className="dialog-heading">
          <h2 id={titleId}>{ko.browserLogin}</h2>
          <Button
            variant="ghost"
            isIconOnly
            autoFocus
            aria-label={ko.close}
            onPress={closeView}
          >
            <X size={22} aria-hidden="true" />
          </Button>
        </div>
        <p id={helpId} className="section-description">
          {ko.browserOfficialLoginHelp}
        </p>
        <div className="form-actions">
          <Button
            isPending={confirming}
            isDisabled={!loginPending || request.isPending || working}
            onPress={completeLogin}
          >
            {ko.browserCompleteLogin}
          </Button>
          <Button
            variant="secondary"
            isPending={request.isPending && request.variables === "login"}
            isDisabled={request.isPending}
            onPress={() => request.mutate("login")}
          >
            {ko.browserFreshLogin}
          </Button>
          <p className="field-help">{ko.browserFreshLoginHelp}</p>
        </div>
        {state && !status.isError && (
          <p className="field-help" role="status" aria-live="polite">
            {ko.browserStates[state]} · {description}
          </p>
        )}
        {request.isError && canView && (
          <p className="error-box" role="alert">
            {ko.browserRequestFailed}
          </p>
        )}
        {canView ? (
          <LoginBrowserView
            onStatusChanged={refreshStatus}
            onRetry={() => request.mutateAsync("open")}
            retryDisabled={request.isPending}
          />
        ) : (
          <div className="pixiv-browser-waiting">
            {request.isPending ||
            status.isPending ||
            (working && !request.isError && !status.isError) ? (
              <p className="pixiv-browser-status" role="status">
                <Spinner size="sm" />
                {ko.browserViewWaiting}
              </p>
            ) : (
              <>
                <p role={request.isError ? "alert" : "status"}>
                  {request.isError
                    ? ko.browserRequestFailed
                    : state === "disabled"
                      ? ko.browserStateDescriptions.disabled
                      : ko.browserViewUnavailable}
                </p>
                {data?.enabled && (
                  <Button
                    variant="secondary"
                    onPress={() => request.mutate("open")}
                  >
                    <RefreshCw size={16} aria-hidden="true" />
                    {ko.retry}
                  </Button>
                )}
              </>
            )}
          </div>
        )}
        <p className="field-help">
          {loginPending ? ko.browserPendingCloseHelp : ko.browserCloseHelp}
        </p>
      </dialog>
    </div>
  );
}

function BrowserTime({ value }: { value: number | null }) {
  return value === null ? (
    <>{ko.browserNever}</>
  ) : (
    <time dateTime={new Date(value * 1000).toISOString()}>
      {new Date(value * 1000).toLocaleString("ko-KR")}
    </time>
  );
}

type ViewState =
  "connecting" | "connected" | "disconnected" | "error" | "session";

function LoginBrowserView({
  onStatusChanged,
  onRetry,
  retryDisabled,
}: {
  onStatusChanged: () => void;
  onRetry: () => Promise<unknown>;
  retryDisabled: boolean;
}) {
  const screen = useRef<HTMLDivElement>(null);
  const connection = useRef<RFB | null>(null);
  const fitValue = useRef(true);
  const keyboardHelpId = useId();
  const [fit, setFit] = useState(true);
  const [attempt, setAttempt] = useState(0);
  const [retrying, setRetrying] = useState(false);
  const [state, setState] = useState<ViewState>("connecting");
  fitValue.current = fit;

  const retry = async () => {
    if (retrying || retryDisabled) return;
    setRetrying(true);
    try {
      // Reopen idempotently, then reconnect only the display channel.
      await onRetry();
      setAttempt((value) => value + 1);
    } catch {
      // The parent shows the request error; leave this screen available to retry.
    } finally {
      setRetrying(false);
    }
  };

  useEffect(() => {
    const target = screen.current;
    if (!target) return;
    setState("connecting");
    let disposed = false;
    let ended = false;
    let rfb: RFB | null = null;
    let socket: WebSocket | null = null;
    let timeout: ReturnType<typeof setTimeout> | undefined;

    const release = (disconnect = true) => {
      clearTimeout(timeout);
      if (rfb) {
        rfb.removeEventListener("connect", connected);
        rfb.removeEventListener("disconnect", disconnected);
        rfb.removeEventListener("securityfailure", failed);
        rfb.removeEventListener("credentialsrequired", failed);
        if (disconnect) rfb.disconnect();
        rfb = null;
      }
      connection.current = null;
      // Also cover failures before RFB has taken ownership of the channel.
      if (socket && socket.readyState < WebSocket.CLOSING) socket.close();
      socket = null;
      target.replaceChildren();
    };
    const finish = (next: ViewState, disconnect = true) => {
      if (disposed || ended) return;
      ended = true;
      setState(next);
      release(disconnect);
      onStatusChanged();
    };
    const connected = () => {
      if (disposed || ended) return;
      clearTimeout(timeout);
      setState("connected");
      onStatusChanged();
    };
    const disconnected = () => finish("disconnected", false);
    const failed = () => finish("error");

    timeout = setTimeout(failed, 30_000);
    void (async () => {
      try {
        const { default: BrowserRFB } = await import("@novnc/novnc");
        if (disposed || ended) return;
        const csrf = getCsrf();
        if (!csrf) {
          finish("session");
          return;
        }
        const url = new URL(
          "/api/accounts/1/browser/view",
          window.location.href,
        );
        url.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        socket = new WebSocket(url, ["binary", `pixchive-csrf.${csrf}`]);
        socket.binaryType = "arraybuffer";
        rfb = new BrowserRFB(target, socket);
        connection.current = rfb;
        rfb.scaleViewport = fitValue.current;
        rfb.resizeSession = false;
        rfb.focusOnClick = true;
        rfb.viewOnly = false;
        rfb.addEventListener("connect", connected);
        rfb.addEventListener("disconnect", disconnected);
        rfb.addEventListener("securityfailure", failed);
        rfb.addEventListener("credentialsrequired", failed);
        const canvas = target.querySelector("canvas");
        if (canvas) {
          canvas.tabIndex = 0;
          canvas.setAttribute("aria-label", ko.browserCanvasLabel);
          canvas.setAttribute("aria-describedby", keyboardHelpId);
        }
      } catch {
        failed();
      }
    })();

    return () => {
      disposed = true;
      release();
    };
  }, [onStatusChanged, keyboardHelpId, attempt]);

  useEffect(() => {
    if (connection.current) connection.current.scaleViewport = fit;
  }, [fit]);

  return (
    <div className="pixiv-browser-view">
      <div className="pixiv-browser-toolbar">
        <span className="field-help" role="status" aria-live="polite">
          {ko.browserViewStates[state]}
        </span>
        <Button
          size="sm"
          variant="secondary"
          onPress={() => setFit((value) => !value)}
        >
          {fit ? ko.browserActualSize : ko.fit}
        </Button>
      </div>
      <div className="pixiv-browser-viewport">
        <div ref={screen} className="pixiv-browser-screen" />
        {state !== "connected" && (
          <div className="pixiv-browser-overlay">
            {state === "connecting" ? (
              <p className="pixiv-browser-status" role="status">
                <Spinner size="sm" />
                {ko.browserViewStates.connecting}
              </p>
            ) : (
              <>
                <p role="alert">
                  {state === "session"
                    ? ko.browserSessionExpired
                    : ko.browserViewFailed}
                </p>
                <Button
                  variant="secondary"
                  isPending={retrying}
                  isDisabled={retrying || retryDisabled}
                  onPress={() => void retry()}
                >
                  <RefreshCw size={16} aria-hidden="true" />
                  {ko.retry}
                </Button>
              </>
            )}
          </div>
        )}
      </div>
      <p id={keyboardHelpId} className="field-help">
        {ko.browserKeyboardHelp}
      </p>
    </div>
  );
}
