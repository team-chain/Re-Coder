export interface PopupPosition { left: number; top: number; maxHeight: number }
/** Keep a measured popup inside the viewport, scrolling inside when neither side fits. */
export function popupPlacement(anchor: { top: number; bottom: number; left: number; right: number },
  viewport: { width: number; height: number }, size: { width: number; height: number },
  align: "left" | "right", belowThreshold: number): PopupPosition {
  const margin = 12, gap = 8;
  const width = Math.min(size.width, Math.max(0, viewport.width - margin * 2));
  const below = Math.max(0, viewport.height - margin - anchor.bottom - gap);
  const above = Math.max(0, anchor.top - gap - margin);
  let down = below >= belowThreshold;
  if (down && size.height > below && size.height <= above) down = false;
  else if (!down && size.height > above && size.height <= below) down = true;
  else if (size.height > below && size.height > above) down = below >= above;
  const maxHeight = Math.min(down ? below : above, Math.max(0, viewport.height - margin * 2));
  const height = Math.min(size.height, maxHeight);
  const top = Math.max(margin, Math.min(down ? anchor.bottom + gap : anchor.top - gap - height, viewport.height - margin - height));
  const left = Math.max(margin, Math.min(align === "right" ? anchor.right - width : anchor.left, viewport.width - width - margin));
  return { left, top, maxHeight };
}
