import assert from "node:assert/strict";
import { test } from "node:test";
import {
  favoriteCollectionPath,
  listingContext,
  scopedParams,
} from "../src/api/library.ts";

test("legacy favorites navigation uses the default collection and preserves filters", () => {
  const context = listingContext(
    "/favorites?q=풍경&page=2&page_size=12&sort=title",
  );
  assert.equal(context.path, favoriteCollectionPath);
  const request = scopedParams(context.params, context.scope);
  assert.equal(request.get("favorite"), "true");
  assert.equal(request.get("q"), "풍경");
  assert.equal(request.get("page"), "2");
  assert.equal(request.get("sort"), "title");
});

test("default collection navigation removes a conflicting custom collection filter", () => {
  const context = listingContext(
    `${favoriteCollectionPath}?collection_id=other&favorite=false&q=풍경&page=2`,
  );
  const request = scopedParams(context.params, context.scope);
  assert.equal(context.path, favoriteCollectionPath);
  assert.equal(request.get("favorite"), "true");
  assert.equal(request.has("collection_id"), false);
  assert.equal(request.get("q"), "풍경");
  assert.equal(request.get("page"), "2");
});

test("collection scope survives detail navigation and overrides another URL list", () => {
  const id = "6f182dcc-d13e-420c-84a4-147cde9ed6ee";
  const context = listingContext(
    `/collections/${id}?collection_id=other&favorite=true&view=large&page=3`,
  );
  const request = scopedParams(context.params, context.scope);
  assert.equal(context.path, `/collections/${id}`);
  assert.equal(request.get("collection_id"), id);
  assert.equal(request.has("favorite"), false);
  assert.equal(request.get("sort"), "created");
  assert.equal(request.has("view"), false);
  assert.equal(request.get("page"), "3");
  assert.equal(
    listingContext("https://other.test/collections/123").path,
    "/library",
  );
});

test("unscoped library keeps intentional combinations of saved filters", () => {
  const context = listingContext("/library?favorite=true&collection_id=custom");
  const request = scopedParams(context.params, context.scope);
  assert.equal(request.get("favorite"), "true");
  assert.equal(request.get("collection_id"), "custom");
});
