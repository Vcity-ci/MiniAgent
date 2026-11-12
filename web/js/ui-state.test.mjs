import test from "node:test";
import assert from "node:assert/strict";

import { isNearBottom, shouldShowUserText } from "./ui-state.js";

test("user text is shown exactly once for text-only and image+text", () => {
  assert.equal(shouldShowUserText("你好", 0), true);
  assert.equal(shouldShowUserText("看图", 2), true);
  assert.equal(shouldShowUserText("（图片）", 1), false);
  assert.equal(shouldShowUserText("", 1), false);
  assert.equal(shouldShowUserText("", 0), true);
});

test("near-bottom detection follows only when close to the bottom", () => {
  assert.equal(isNearBottom({ scrollTop: 900, scrollHeight: 1000, clientHeight: 100 }), true);
  assert.equal(isNearBottom({ scrollTop: 0, scrollHeight: 1000, clientHeight: 100 }), false);
  assert.equal(isNearBottom({ scrollTop: 0, scrollHeight: 0, clientHeight: 0 }), true);
  assert.equal(
    isNearBottom({ scrollTop: 930, scrollHeight: 1000, clientHeight: 100 }, 48),
    true
  );
  assert.equal(
    isNearBottom({ scrollTop: 800, scrollHeight: 1000, clientHeight: 100 }, 48),
    false
  );
});
