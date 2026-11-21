// The preview displays captured worker pixels; it never navigates a second
// platform page and therefore cannot show a different recommendation stream.
async function updateWorkerPreview(entry, state, agent, previewUrl, log = () => {}) {
  if (!entry.attached || !state || (!state.busy && !entry.__workerPreview) || entry.__capturing) return;
  entry.__capturing = true;
  try {
    if (entry.view.webContents.getURL() !== previewUrl) {
      await entry.view.webContents.loadURL(previewUrl);
      entry.__workerPreview = true;
      log('实况切换真实执行画面');
    }
    if (!entry.attached) return;
    if (state.busy) {
      entry.__previewBusy = true;
      const frame = await agent.captureWorkerFrame(entry.accountId);
      if (frame && entry.attached && entry.view.webContents.getURL() === previewUrl) {
        const shown = await entry.view.webContents.executeJavaScript('window.showWorkerFrame(' + JSON.stringify(frame) + ')');
        if (shown?.displayed === true && shown.capturedAt === frame.capturedAt && entry.attached) {
          entry.__displayedFrameAt = shown.capturedAt;
          entry.__displayedRunId = frame.runId || "";
        }
      }
    } else if (entry.__previewBusy !== false) {
      await entry.view.webContents.executeJavaScript('window.endWorkerPreview()');
      entry.__previewBusy = false;
    }
    entry.__previewError = false;
  } catch (error) {
    if (!entry.__previewError) log('实况画面读取失败', error.message);
    entry.__previewError = true;
  } finally { entry.__capturing = false; }
}

// Renderer rectangles are CSS pixels. BrowserView bounds use window DIPs;
// devicePixelRatio also includes monitor scaling and must not be used here.
function cssToViewBounds(rect, hostZoom = 1) {
  const zoom = Number.isFinite(hostZoom) && hostZoom > 0 ? hostZoom : 1;
  if (!rect || !['x', 'y', 'width', 'height'].every(key => Number.isFinite(rect[key]))) return null;
  return {x: Math.round(rect.x * zoom), y: Math.round(rect.y * zoom),
    width: Math.max(1, Math.round(rect.width * zoom)), height: Math.max(1, Math.round(rect.height * zoom))};
}

function isWorkerPreview(webContents) {
  try { return new URL(webContents.getURL()).pathname === '/ui/live-worker-preview.html'; }
  catch { return false; }
}

// Shared by login, nurture and every task dashboard. A key prefix must never
// change the presentation. Keep the whole execution page inside its own host.
function setAutomationLayout(entry, payload = {}) {
  const bounds = payload.bounds;
  if (!bounds || !['x','y','width','height'].every(key => Number.isFinite(bounds[key])) ||
      bounds.width <= 2 || bounds.height <= 2) return false;
  entry.bounds = {...bounds};
  const zoom = payload.zoomFactor;
  entry.zoomFactor = Number.isFinite(zoom) && zoom > .1 && zoom < 5 ? zoom :
    Math.max(.25, Math.min(1, bounds.width / 1280, bounds.height / 800));
  entry.surface?.setPresentation('page');
  return true;
}

function applyViewBounds(entry, hostZoom = 1) {
  if (!entry.attached || !entry.bounds) return;
  const bounds = cssToViewBounds(entry.bounds, hostZoom), previous = entry.__appliedBounds;
  if (!bounds) return;
  if (!previous || ['x','y','width','height'].some(key => bounds[key] !== previous[key])) {
    entry.view.setBounds(bounds);
    entry.__appliedBounds = {...bounds};
  }
  // The canvas already fits its host. Platform-page zoom would affect the
  // same-origin main UI too, shrinking the layout and feeding back into bounds.
  // Check the URL as well as the flag while returning to an interactive view.
  if (entry.__workerPreview || isWorkerPreview(entry.view.webContents)) return;
  const zoom = entry.zoomFactor;
  if (zoom > 0.1 && zoom < 5 && zoom !== entry.__appliedZoomFactor) {
    if(entry.surface?.setPageZoom)entry.surface.setPageZoom(zoom);
    else entry.view.webContents.setZoomFactor(zoom);
    entry.__appliedZoomFactor = zoom;
  }
}

module.exports = {updateWorkerPreview, applyViewBounds, cssToViewBounds, setAutomationLayout};
