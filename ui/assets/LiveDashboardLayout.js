// Every task uses the same complete-page layout as the nurture dashboard.
export function dashboardPageZoom(bounds) {
  return bounds ? Math.max(.25, Math.min(1, bounds.width / 1280, bounds.height / 800)) : 1;
}

export function dashboardGrid(bounds, count, gap = 6) {
  if (!bounds || !Number.isFinite(count) || count < 1) return [];
  const columns = count <= 1 ? 1 : count <= 4 ? 2 : count <= 9 ? 3 : 4;
  const rows = Math.ceil(count / columns);
  const width = Math.max(1, Math.floor((bounds.width - gap * (columns + 1)) / columns));
  const height = Math.max(1, Math.floor((bounds.height - gap * (rows + 1)) / rows));
  return Array.from({length: count}, (_, index) => ({
    x: (bounds.x || 0) + gap + index % columns * (width + gap),
    y: (bounds.y || 0) + gap + Math.floor(index / columns) * (height + gap),
    width, height,
  }));
}
