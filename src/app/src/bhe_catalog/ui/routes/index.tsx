import { createFileRoute, Navigate } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { fetchSetupStatus } from "@/lib/api-client";

export const Route = createFileRoute("/")({
  component: HomeRedirect,
});

/**
 * First-load routing:
 *  - If the app is not yet bootstrapped (schemas/tables missing or the
 *    service principal can't reach the catalog/warehouse), drop the user on
 *    /company so they hit the setup wizard immediately.
 *  - Once setup is "data-ready" (all infra checks green + at least one row
 *    of silver_schemas), send them to the dashboard like before.
 *  - While the status query is in flight, render a tiny placeholder to avoid
 *    flashing the dashboard for a fraction of a second.
 */
function HomeRedirect() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["setupStatus"],
    queryFn: fetchSetupStatus,
    // Don't keep retrying on a permissions failure -- the wizard will
    // show the actual error message when we land on /company.
    retry: false,
    staleTime: 30_000,
  });

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-screen text-sm text-muted-foreground">
        Loading…
      </div>
    );
  }

  // Any error reaching /api/setup/status -> safest to land on the wizard
  // which is designed to render even when the backend can't talk to UC yet.
  if (isError || !data) {
    return <Navigate to="/company" />;
  }

  return <Navigate to={data.is_data_ready ? "/dashboard" : "/company"} />;
}
