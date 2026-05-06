import SidebarLayout from "@/components/apx/sidebar-layout";
import { createFileRoute, Link, useLocation } from "@tanstack/react-router";
import { cn } from "@/lib/utils";
import {
  LayoutDashboard,
  Building2,
  PenSquare,
  User,
  BarChart3,
  Settings2,
  Grid3X3,
  BookOpen,
  Boxes,
  TrendingUp,
  Info,
  AlertTriangle,
  FileBarChart,
  Library,
  Lightbulb,
} from "lucide-react";
import {
  SidebarGroup,
  SidebarGroupLabel,
  SidebarGroupContent,
  SidebarMenu,
  SidebarMenuItem,
} from "@/components/ui/sidebar";

export const Route = createFileRoute("/_sidebar")({
  component: () => <Layout />,
});

function Layout() {
  const location = useLocation();

  const catalogNav = [
    {
      to: "/data-catalog",
      label: "Data Catalog",
      icon: <BookOpen size={16} />,
      match: (path: string) => path === "/data-catalog",
    },
    {
      to: "/source-systems",
      label: "Source Systems",
      icon: <Boxes size={16} />,
      match: (path: string) => path.startsWith("/source-systems"),
    },
    {
      to: "/artifacts",
      label: "BI & AI Artifacts",
      icon: <FileBarChart size={16} />,
      match: (path: string) => path.startsWith("/artifacts"),
    },
    {
      to: "/knowledge",
      label: "Knowledge",
      icon: <Library size={16} />,
      match: (path: string) => path.startsWith("/knowledge"),
    },
  ];

  const analyticsNav = [
    {
      to: "/dashboard",
      label: "Dashboard",
      icon: <LayoutDashboard size={16} />,
      match: (path: string) => path === "/dashboard",
    },
    {
      to: "/use-cases",
      label: "Use Cases",
      icon: <Lightbulb size={16} />,
      match: (path: string) => path.startsWith("/use-cases"),
    },
    {
      to: "/value-readiness",
      label: "Value & Readiness",
      icon: <TrendingUp size={16} />,
      match: (path: string) => path.startsWith("/value-readiness"),
    },
    {
      to: "/gaps",
      label: "Gaps",
      icon: <AlertTriangle size={16} />,
      match: (path: string) => path.startsWith("/gaps"),
    },
    {
      to: "/analytics",
      label: "Platform Analytics",
      icon: <BarChart3 size={16} />,
      match: (path: string) => path === "/analytics",
    },
    {
      to: "/taxonomy",
      label: "Source Taxonomy",
      icon: <Grid3X3 size={16} />,
      match: (path: string) => path === "/taxonomy",
    },
  ];

  const managementNav = [
    {
      to: "/company",
      label: "Company Setup",
      icon: <Building2 size={16} />,
      match: (path: string) => path === "/company",
    },
    {
      to: "/rules",
      label: "Classification Rules",
      icon: <Settings2 size={16} />,
      match: (path: string) => path === "/rules",
    },
    {
      to: "/edit",
      label: "Edit Center",
      icon: <PenSquare size={16} />,
      match: (path: string) => path === "/edit",
    },
    {
      to: "/profile",
      label: "Profile",
      icon: <User size={16} />,
      match: (path: string) => path === "/profile",
    },
  ];

  const aboutNav = [
    {
      to: "/about",
      label: "About",
      icon: <Info size={16} />,
      match: (path: string) => path.startsWith("/about"),
    },
  ];

  const renderNav = (items: typeof mainNav) => (
    <SidebarMenu>
      {items.map((item) => (
        <SidebarMenuItem key={item.to}>
          <Link
            to={item.to}
            className={cn(
              "flex items-center gap-2 p-2 rounded-lg",
              item.match(location.pathname)
                ? "bg-sidebar-accent text-sidebar-accent-foreground"
                : "text-sidebar-foreground hover:bg-sidebar-accent hover:text-sidebar-accent-foreground",
            )}
          >
            {item.icon}
            <span>{item.label}</span>
          </Link>
        </SidebarMenuItem>
      ))}
    </SidebarMenu>
  );

  return (
    <SidebarLayout>
      <SidebarGroup>
        <SidebarGroupLabel>Data Catalog</SidebarGroupLabel>
        <SidebarGroupContent>{renderNav(catalogNav)}</SidebarGroupContent>
      </SidebarGroup>
      <SidebarGroup>
        <SidebarGroupLabel>Insights</SidebarGroupLabel>
        <SidebarGroupContent>{renderNav(analyticsNav)}</SidebarGroupContent>
      </SidebarGroup>
      <SidebarGroup>
        <SidebarGroupLabel>Management</SidebarGroupLabel>
        <SidebarGroupContent>{renderNav(managementNav)}</SidebarGroupContent>
      </SidebarGroup>
      <SidebarGroup>
        <SidebarGroupLabel>Documentation</SidebarGroupLabel>
        <SidebarGroupContent>{renderNav(aboutNav)}</SidebarGroupContent>
      </SidebarGroup>
    </SidebarLayout>
  );
}
