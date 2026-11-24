export function dataUrl(path, base = import.meta.env?.BASE_URL ?? '/') {
  return `${base}data/${path}`;
}
