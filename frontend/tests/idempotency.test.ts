import assert from "node:assert/strict";
import { test } from "node:test";
import { newIdempotencyKey } from "../src/api/idempotency.ts";

test("HTTP crypto without randomUUID still creates a request key", () => {
  const original = Object.getOwnPropertyDescriptor(globalThis, "crypto")!;
  let calls = 0;
  Object.defineProperty(globalThis, "crypto", {
    configurable: true,
    value: {
      getRandomValues(bytes: Uint8Array) {
        calls++;
        assert.equal(bytes.length, 16);
        for (let index = 0; index < bytes.length; index++) bytes[index] = index;
        return bytes;
      },
    },
  });
  try {
    assert.equal(newIdempotencyKey(), "000102030405060708090a0b0c0d0e0f");
    assert.equal(calls, 1);
  } finally {
    Object.defineProperty(globalThis, "crypto", original);
  }
});

test("separate actions receive distinct keys that satisfy the API header limit", () => {
  const keys = Array.from({ length: 1000 }, () => newIdempotencyKey());
  assert.equal(new Set(keys).size, keys.length);
  assert.ok(keys.every((key) => /^[0-9a-f]{32}$/.test(key)));
});
