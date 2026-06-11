const assert = require("node:assert");
const test = require("node:test");
const { createApiAuth } = require("../lib/auth");

function req({ headerToken = "", bearer = "", queryToken = "" } = {}) {
  return {
    query: queryToken ? { token: queryToken } : {},
    get(name) {
      if (name === "x-api-token") return headerToken;
      if (name === "authorization") return bearer;
      return "";
    }
  };
}

function res() {
  return {
    code: 0,
    body: null,
    status(code) {
      this.code = code;
      return this;
    },
    json(body) {
      this.body = body;
    }
  };
}

test("api auth is optional when no token is configured", () => {
  const auth = createApiAuth({});
  let called = false;
  auth.middleware(req(), res(), () => { called = true; });
  assert.equal(auth.publicInfo().enabled, false);
  assert.equal(called, true);
});

test("api auth rejects missing token and accepts supported token locations", () => {
  const auth = createApiAuth({ API_TOKEN: "secret" });
  let called = false;
  const missing = res();
  auth.middleware(req(), missing, () => { called = true; });
  assert.equal(called, false);
  assert.equal(missing.code, 401);

  for (const request of [
    req({ headerToken: "secret" }),
    req({ bearer: "Bearer secret" }),
    req({ queryToken: "secret" })
  ]) {
    called = false;
    auth.middleware(request, res(), () => { called = true; });
    assert.equal(called, true);
  }
});
