// 极简 history 路由：事故列表、事故详情、评测页，不引入路由库。
import { useSyncExternalStore } from "react";

const listeners = new Set<() => void>();

function notify() {
  listeners.forEach((l) => l());
}

window.addEventListener("popstate", notify);

export function navigate(path: string, { replace = false }: { replace?: boolean } = {}) {
  const current = window.location.pathname + window.location.search;
  if (path !== current) {
    if (replace) window.history.replaceState(null, "", path);
    else window.history.pushState(null, "", path);
    notify();
  }
}

export function usePath(): string {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => window.location.pathname,
  );
}
