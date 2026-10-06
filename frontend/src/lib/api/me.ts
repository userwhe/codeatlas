import { useQuery } from "@tanstack/react-query";

import { api, unwrap } from "./client";

/** The signed-in user and workspace. The query fails with a 401 `ApiError` when anonymous. */
export function useMe() {
  return useQuery({
    queryKey: ["me"],
    queryFn: () => unwrap(api.GET("/v1/me")),
  });
}
