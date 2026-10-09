import { defineRailway, project, service } from "railway/iac";

// Last resort for a per-service CaC repo. Prefer one .railway file for the
// project and drop this if you later combine services into that file.
export const partial = "lockstep";

export default defineRailway(() => {
  const lockstep = service("lockstep", {
    replicas: 1,
    // dockerfilePath from CaC: "deploy/Dockerfile"
    // builder from CaC: "DOCKERFILE"
  });
  return project("lockstep", {
    resources: [lockstep],
  });
});
