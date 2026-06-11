const crypto = require("crypto");

const CREDENTIALS = { sl: "sl,123321" };
const WEB_SESSION_TOKEN = process.env.WEB_SESSION_TOKEN || crypto.randomBytes(32).toString("hex");

function handleLogin(username, password) {
  const user = String(username || "").trim().toLowerCase();
  const pwd = String(password || "").trim();
  if (CREDENTIALS[user] && CREDENTIALS[user] === pwd) {
    return { success: true, token: WEB_SESSION_TOKEN, username: user };
  }
  return { success: false, error: "账号或密码不正确" };
}

function firstNonEmpty(values) {
  for (const value of values) {
    if (value !== undefined && value !== null && String(value).trim()) return String(value).trim();
  }
  return undefined;
}

function tokenFromRequest(req) {
  var headers = req.headers || {};
  var auth = headers["authorization"];
  if (auth && auth.startsWith("Bearer ")) return auth.slice(7).trim();

  var header = headers["x-api-token"];
  if (header) return String(header).trim();

  if (req.query && req.query.token) return String(req.query.token).trim();

  // Fallback: support Express req.get() for test mocks that use it
  if (typeof req.get === "function") {
    try {
      var auth2 = req.get("authorization");
      if (auth2 && auth2.startsWith("Bearer ")) return auth2.slice(7).trim();
      var header2 = req.get("x-api-token");
      if (header2) return String(header2).trim();
    } catch (e) {}
  }

  return undefined;
}

function createApiAuth(env = process.env) {
  const token = firstNonEmpty([
    env.API_TOKEN,
    env.CODEX_API_TOKEN,
    env.TRADE_API_TOKEN
  ]);

  function middleware(req, res, next) {
    const reqToken = tokenFromRequest(req);

    if (reqToken === WEB_SESSION_TOKEN) {
      next();
      return;
    }

    if (!token) {
      next();
      return;
    }
    if (reqToken === token) {
      next();
      return;
    }
    res.status(401).json({ error: "invalid or missing api token" });
  }

  return {
    middleware,
    publicInfo() {
      return {
        enabled: !!token || Object.keys(CREDENTIALS).length > 0,
        header: "X-API-Token",
        bearer: true,
        queryParam: "token"
      };
    }
  };
}

module.exports = { createApiAuth, handleLogin, WEB_SESSION_TOKEN };
