// Client authentication: Authorization: Bearer <token> matched against every TOKEN_<CLIENT> secret.
import { equalBytes, sha256 } from "./util.js";

const TOKEN_NAME = /^TOKEN_([A-Z0-9_]{1,32})$/;
export const MIN_TOKEN_LENGTH = 32;

/**
 * Returns the client id (lowercase suffix of the matching TOKEN_* secret) or null.
 * Both sides are hashed to fixed-length digests and compared with an XOR accumulator;
 * every configured secret is checked, with no early exit.
 */
export async function matchToken(presented, env) {
  if (typeof presented !== "string" || presented.length === 0) return null;
  const want = await sha256(presented);
  let found = null;
  for (const name of Object.keys(env || {}).sort()) {
    const m = TOKEN_NAME.exec(name);
    if (!m) continue;
    const value = env[name];
    if (typeof value !== "string" || value.length < MIN_TOKEN_LENGTH) continue;
    const have = await sha256(value);
    const same = equalBytes(want, have);
    if (same && found === null) found = m[1].toLowerCase();
  }
  return found;
}

export async function authenticate(request, env) {
  const header = request.headers.get("Authorization") || "";
  const m = /^Bearer[ \t]+(\S+)[ \t]*$/i.exec(header);
  if (!m) return null;
  return matchToken(m[1], env);
}
