// Small, DOM-free UI state helpers.

export function shouldShowUserText(userText, imageCount = 0) {
  return Boolean(userText && userText !== "（图片）") || Number(imageCount) === 0;
}

export function isNearBottom(
  { scrollTop = 0, scrollHeight = 0, clientHeight = 0 } = {},
  threshold = 48
) {
  const gap = Number(scrollHeight) - Number(scrollTop) - Number(clientHeight);
  return gap <= threshold;
}
