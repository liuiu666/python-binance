function firstNonEmpty(values) {
  for (const value of values) {
    if (value !== undefined && value !== null && String(value).trim()) return String(value).trim();
  }
  return "";
}

function createApiAuth(env = process.env) {
  const token = firstNonEmpty([
    env.API_TOKEN,
    env.CODEX_API_TOKEN,
    env.TRADE_API_TOKEN
  ]);

  function tokenFromRequest(req) {
    const headerToken = req.get("x-api-token");
    if (headerToken) return String(headerToken).trim();
    const auth = req.get("authorization") || "";
    const m = auth.match(/^Bearer\s+(.+)$/i);
    if (m) return m[1].trim();
    if (req.query && req.query.token) return String(req.query.token).trim();
    return "";
  }

  function middleware(req, res, next) {
    if (!token) {
      next();
      return;
    }
    if (tokenFromRequest(req) === token) {
      next();
      return;
    }
    res.status(401).json({ error: "unauthorized", authRequired: true });
  }

  return {
    enabled: !!token,
    middleware,
    publicInfo() {
      return {
        enabled: !!token,
        header: "X-API-Token",
        bearer: true,
        queryParam: "token"
      };
    }
  };
}

module.exports = { createApiAuth };
