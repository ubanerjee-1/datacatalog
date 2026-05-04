import { useQuery, useSuspenseQuery } from "@tanstack/react-query";
import axios from "axios";

const api = axios.create({
  baseURL: "/api",
  headers: { "Content-Type": "application/json" },
});

async function fetchCurrentUser() {
  const { data } = await api.get("/current-user");
  return { data };
}

export function useCurrentUser(options?: { query?: Record<string, unknown> }) {
  return useQuery({
    queryKey: ["currentUser"],
    queryFn: fetchCurrentUser,
    ...options?.query,
  });
}

export function useCurrentUserSuspense(options?: {
  query?: Record<string, unknown>;
}) {
  return useSuspenseQuery({
    queryKey: ["currentUser"],
    queryFn: fetchCurrentUser,
    ...options?.query,
  });
}
