history.scrollRestoration = "manual";
import React, { Suspense, lazy, useEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import {
  BrowserRouter,
  Navigate,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";
import {
  QueryClient,
  QueryClientProvider,
  useQuery,
} from "@tanstack/react-query";
import { api, setCsrf, type Models } from "./api/client";
import { favoriteCollectionPath } from "./api/library";
import { Loading, ErrorState } from "./components/Common";
import { Shell } from "./components/Shell";
import { ScreenErrorBoundary } from "./components/ScreenErrorBoundary";
import { Auth } from "./pages/Auth";
import { Library } from "./pages/Library";
import {
  SavedCollections,
  SavedCollectionPage,
} from "./pages/SavedCollections";
import { Directory } from "./pages/Directory";
import { ArtistPage, TagPage } from "./pages/EntityDirectory";
import { Jobs } from "./pages/Jobs";
import { SettingsPage } from "./pages/Settings";
import "@fontsource-variable/noto-sans-kr";
import "./styles/app.css";
import "./styles/saved-collections.css";

const JobDetail = lazy(() =>
  import("./pages/JobDetail").then((module) => ({ default: module.JobDetail })),
);
const Detail = lazy(() =>
  import("./pages/Detail").then((module) => ({ default: module.Detail })),
);
const SetupPage = lazy(() =>
  import("./pages/Setup").then((module) => ({ default: module.SetupPage })),
);

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false, staleTime: 10_000 } },
});

async function resetSession() {
  setCsrf(null);
  await queryClient.cancelQueries();
  queryClient.removeQueries({
    predicate: (query) => query.queryKey[0] !== "session",
  });
  queryClient.setQueryData(["session"], {
    authenticated: false,
    setup_required: false,
    csrf_token: null,
    username: null,
  } satisfies Models["SessionStatus"]);
}

function FavoritesRedirect() {
  const location = useLocation();
  return (
    <Navigate
      to={`${favoriteCollectionPath}${location.search}${location.hash}`}
      state={location.state}
      replace
    />
  );
}

function App() {
  const navigate = useNavigate();
  // Where the catch-all route sends a signed-in visitor. Right after the first
  // administrator is created this is the setup guide: the router applies
  // navigate() as a transition, so the first signed-in render can still see
  // "/" and would otherwise redirect to the library instead.
  const [landing, setLanding] = useState("/library");
  const session = useQuery({
    queryKey: ["session"],
    queryFn: () => api<Models["SessionStatus"]>("/session"),
    staleTime: Infinity,
  });
  useEffect(() => {
    const expire = () => {
      void resetSession();
    };
    window.addEventListener("session-expired", expire);
    return () => window.removeEventListener("session-expired", expire);
  }, []);
  if (session.isPending) return <Loading />;
  if (session.error)
    return (
      <ErrorState error={session.error} retry={() => void session.refetch()} />
    );
  setCsrf(session.data.csrf_token);
  if (!session.data.authenticated)
    return (
      <Auth
        setup={session.data.setup_required}
        onSuccess={(data) => {
          setCsrf(data.csrf_token);
          setLanding(session.data.setup_required ? "/setup" : "/library");
          queryClient.setQueryData(["session"], data);
          if (session.data.setup_required)
            navigate("/setup", { replace: true });
        }}
      />
    );
  const logout = async () => {
    try {
      await api("/session", { method: "DELETE" });
      await resetSession();
    } catch {
      await session.refetch();
    }
  };
  return (
    <Routes>
      <Route
        element={
          <Shell
            username={session.data.username ?? ""}
            logout={() => void logout()}
          />
        }
      >
        <Route path="/library" element={<Library />} />
        <Route path="/favorites" element={<FavoritesRedirect />} />
        <Route path="/collections" element={<SavedCollections />} />
        <Route path="/collections/:id" element={<SavedCollectionPage />} />
        <Route
          path="/setup"
          element={
            <Suspense fallback={<Loading />}>
              <SetupPage />
            </Suspense>
          }
        />
        <Route
          path="/artworks/:id"
          element={
            <Suspense fallback={<Loading />}>
              <Detail />
            </Suspense>
          }
        />
        <Route path="/artists" element={<Directory kind="artists" />} />
        <Route path="/artists/:id" element={<ArtistPage />} />
        <Route path="/tags" element={<Directory kind="tags" />} />
        <Route path="/tags/:id" element={<TagPage />} />
        <Route path="/jobs" element={<Jobs />} />
        <Route
          path="/jobs/:id"
          element={
            <Suspense fallback={<Loading />}>
              <JobDetail />
            </Suspense>
          }
        />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to={landing} replace />} />
      </Route>
    </Routes>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <ScreenErrorBoundary>
          <App />
        </ScreenErrorBoundary>
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);
