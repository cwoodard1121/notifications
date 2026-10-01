// Cloudflare glue only. All behaviour lives in core.js (testable in plain Node).
import { DurableObject } from "cloudflare:workers";
import { Gateway, frontDoor } from "./core.js";

export class PhoneGateway extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.gateway = new Gateway({
      storage: ctx.storage,
      env,
      now: () => Date.now(),
      fetch: (input, init) => fetch(input, init),
    });
  }

  fetch(request) {
    return this.gateway.handle(request);
  }

  alarm() {
    return this.gateway.alarm();
  }
}

export default {
  async fetch(request, env) {
    const early = frontDoor(request, env);
    if (early) return early;
    // One instance owns all state (one phone line, one queue).
    const stub = env.GATEWAY.get(env.GATEWAY.idFromName("owner"));
    return stub.fetch(request);
  },
};
