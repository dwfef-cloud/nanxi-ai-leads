// Keep the canvas pixels intact while the next image is loading/decoding.
// Never expose an undecoded image or clear the old frame between captures.
let lastFrameAt = 0, running = false, frameRevision = 0;
const canvas = document.getElementById('frame');
const context = canvas.getContext('2d', {alpha:false});
window.showWorkerFrame = async frame => {
  if (!frame || !/^data:image\/jpeg;base64,/.test(frame.image) ||
      !Number.isFinite(frame.capturedAt) || frame.capturedAt <= lastFrameAt) return;
  const revision = ++frameRevision;
  const image = new Image();
  image.src = frame.image;
  try { await image.decode(); } catch (_) { return; }
  if (revision !== frameRevision || !image.naturalWidth || !image.naturalHeight) return;
  // Resizing clears a canvas, so resize only when its intrinsic size changes.
  // Resize and draw happen in the same task; there is no empty intervening paint.
  if (canvas.width !== image.naturalWidth) canvas.width = image.naturalWidth;
  if (canvas.height !== image.naturalHeight) canvas.height = image.naturalHeight;
  context.drawImage(image, 0, 0);
  if (canvas.style.display !== 'block') {
    canvas.style.display = 'block';
    document.getElementById('waiting').style.display = 'none';
  }
  lastFrameAt = frame.capturedAt;
  running = !!frame.busy;
  const status = running ? '执行中 · 实时画面' : '执行窗口空闲 · 最后画面';
  if (document.getElementById('status').textContent !== status) document.getElementById('status').textContent = status;
  const time = new Date(lastFrameAt).toLocaleTimeString('zh-CN', {hour12:false});
  if (document.getElementById('at').textContent !== time) document.getElementById('at').textContent = time;
  return {displayed: true, capturedAt: lastFrameAt};
};
window.endWorkerPreview = () => {
  frameRevision++;
  running = false;
  document.getElementById('status').textContent = '执行窗口空闲 · 最后画面';
};
setInterval(() => {
  if (running && lastFrameAt && Date.now() - lastFrameAt > 5000) {
    document.getElementById('status').textContent = '画面更新中断，正在重新连接…';
  }
}, 1000);
