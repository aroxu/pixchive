import { Button } from "@heroui/react";
import {
  Bookmark,
  Grid2X2,
  Users,
  Tags,
  Activity,
  Settings,
  LogOut,
  HardDrive,
  ArrowUpRight,
} from "lucide-react";
import { NavLink, Outlet } from "react-router-dom";
import { ko } from "../i18n/ko";

const links = [
  ["/library", ko.library, Grid2X2],
  ["/artists", ko.artists, Users],
  ["/tags", ko.tags, Tags],
  ["/jobs", ko.jobs, Activity],
  ["/settings", ko.settings, Settings],
] as const;

export function Shell({
  logout,
  username,
}: {
  logout: () => void;
  username: string;
}) {
  return (
    <div className="app-layout">
      <a className="skip-link" href="#main">
        {ko.skip}
      </a>
      <aside className="sidebar">
        <NavLink to="/library" className="brand">
          <span className="brand-mark">
            <Bookmark size={23} fill="currentColor" />
          </span>
          <span>
            {ko.brand}
            <small>{ko.archive}</small>
          </span>
        </NavLink>
        <Button
          className="mobile-logout"
          variant="ghost"
          isIconOnly
          aria-label={ko.logout}
          onPress={logout}
        >
          <LogOut size={18} />
        </Button>
        <div className="sidebar-section-label">{ko.collection}</div>
        <nav aria-label={ko.navigation}>
          {links.map(([to, label, Icon]) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                `nav-item ${isActive ? "active" : ""}`
              }
            >
              <Icon size={19} />
              <span>{label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="local-note">
            <HardDrive size={19} />
            <p>{ko.localNotice}</p>
            <ArrowUpRight size={15} />
          </div>
          <div className="profile">
            <span className="profile-avatar">
              {username.slice(0, 1).toUpperCase()}
            </span>
            <span>
              {username}
              <small>{ko.archive}</small>
            </span>
            <Button
              variant="ghost"
              isIconOnly
              aria-label={ko.logout}
              onPress={logout}
            >
              <LogOut size={17} />
            </Button>
          </div>
        </div>
      </aside>
      <main id="main" className="main-content">
        <Outlet />
      </main>
    </div>
  );
}
