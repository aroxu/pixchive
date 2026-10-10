import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, body, type Models } from "../api/client";

export function useTagRefresh() {
  const client = useQueryClient();
  return () => {
    for (const key of ["tags", "artworks", "artwork", "neighbors", "artists"])
      void client.invalidateQueries({ queryKey: [key] });
  };
}

export function useArtworkTagEdit(
  onSuccess?: (request: Models["TagEdit"]) => void,
) {
  const refresh = useTagRefresh();
  return useMutation({
    mutationFn: (request: Models["TagEdit"]) =>
      api<void>("/artworks/tags/bulk", {
        method: "POST",
        body: body(request),
      }),
    onSuccess: (_, request) => {
      refresh();
      onSuccess?.(request);
    },
  });
}
