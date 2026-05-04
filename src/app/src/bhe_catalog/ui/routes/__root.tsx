import { ChatLauncher } from "@/components/chat/chat-launcher";
import { ThemeProvider } from "@/components/apx/theme-provider";
import { QueryClient } from "@tanstack/react-query";
import { createRootRouteWithContext, Outlet } from "@tanstack/react-router";
import { Toaster } from "sonner";

export const Route = createRootRouteWithContext<{
  queryClient: QueryClient;
}>()({
  component: () => (
    <ThemeProvider defaultTheme="dark" storageKey="apx-ui-theme">
      <Outlet />
      {/* Global chatbot — bottom-left FAB visible on every route. */}
      <ChatLauncher />
      <Toaster richColors />
    </ThemeProvider>
  ),
});
