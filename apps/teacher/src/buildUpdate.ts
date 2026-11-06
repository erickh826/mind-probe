import { createVersionMonitor } from "@mind-probe/shared";

/** Reports a newer deployed build. Silent on the dev server. */
export const buildUpdate = createVersionMonitor({
  currentBuildId: __APP_BUILD_ID__,
  enabled: import.meta.env.PROD,
});
