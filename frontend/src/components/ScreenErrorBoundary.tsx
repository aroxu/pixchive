import { Button } from "@heroui/react";
import { Component, type ReactNode } from "react";
import { ko } from "../i18n/ko";

export class ScreenErrorBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <main className="screen-recovery">
        <section className="error-box" role="alert">
          <h1>{ko.screenUnavailable}</h1>
          <p>{ko.screenRecoveryHelp}</p>
          <div className="form-actions">
            <Button
              variant="secondary"
              onPress={() => window.location.reload()}
            >
              {ko.reloadPage}
            </Button>
            <a className="text-link" href="/library">
              {ko.returnToLibrary}
            </a>
          </div>
        </section>
      </main>
    );
  }
}
