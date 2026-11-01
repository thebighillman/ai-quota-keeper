import { createRouter, publicQuery } from "./middleware";
import { quotaRouter } from "./quotaRouter";

export const appRouter = createRouter({
  ping: publicQuery.query(() => ({ ok: true, ts: Date.now() })),
  quota: quotaRouter,
});

export type AppRouter = typeof appRouter;
