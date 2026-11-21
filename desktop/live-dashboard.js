"use strict";

// A visible frame must come from this preparation, never from a previous run.
async function waitForVisiblePreview(io, {timeoutMs = 15000} = {}) {
  const startedAt = io.now();
  while (io.now() - startedAt < timeoutMs) {
    if (io.stopped()) return false;
    const frame = await io.frame();
    if (frame?.visible && frame.capturedAt >= startedAt && !io.stopped()) return true;
    await io.sleep(150);
  }
  return false;
}
module.exports = {waitForVisiblePreview};
