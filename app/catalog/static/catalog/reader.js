const root = document.querySelector('#course-reader');
const action = name => root.querySelector('[data-action="' + name + '"]');
const status = root.querySelector('#reader-status');
const pages = root.querySelector('#reader-pages');
const viewport = root.querySelector('.reader-scroll');
const canvas = viewport.querySelector('canvas');
const recovery = root.querySelector('#reader-recovery');
let documentPdf, page = 1, zoom = 1, rotation = 0, revision = 0;

function controls(disabled) {
  root.querySelectorAll('[data-action]').forEach(button => { button.disabled = disabled; });
  if (!disabled) {
    action('prev').disabled = page <= 1;
    action('next').disabled = page >= documentPdf.numPages;
    action('zoom-out').disabled = zoom <= .5;
    action('zoom-in').disabled = zoom >= 3;
  }
}

async function paint() {
  if (!documentPdf) return;
  const current = ++revision;
  controls(true);
  status.textContent = '正在绘制第 ' + page + ' 页……';
  try {
    const sheet = await documentPdf.getPage(page);
    const turn = (sheet.rotate + rotation) % 360;
    const normal = sheet.getViewport({scale: 1, rotation: turn});
    const width = Math.max(120, Math.min(1400, root.clientWidth - 2)) * zoom;
    const cssScale = width / normal.width;
    const cssHeight = normal.height * cssScale;
    const scale = Math.min(cssScale * Math.min(devicePixelRatio || 1, 2),
      3200 / Math.max(normal.width, normal.height),
      Math.sqrt(12_000_000 / (normal.width * normal.height)));
    const shape = sheet.getViewport({scale, rotation: turn});
    const frame = document.createElement('canvas');
    frame.width = Math.max(1, Math.ceil(shape.width));
    frame.height = Math.max(1, Math.ceil(shape.height));
    await sheet.render({canvasContext: frame.getContext('2d'), viewport: shape}).promise;
    if (current !== revision) return;
    canvas.width = frame.width;
    canvas.height = frame.height;
    canvas.getContext('2d').drawImage(frame, 0, 0);
    canvas.style.width = width + 'px';
    canvas.style.height = cssHeight + 'px';
    canvas.setAttribute('aria-label', 'PDF 第 ' + page + ' 页');
    viewport.hidden = false;
    recovery.hidden = true;
    pages.textContent = page + ' / ' + documentPdf.numPages;
    status.textContent = '第 ' + page + ' 页，共 ' + documentPdf.numPages + ' 页 · ' + Math.round(zoom * 100) + '%';
  } catch {
    if (current !== revision) return;
    viewport.hidden = true;
    recovery.hidden = false;
    status.textContent = '这一页暂时无法显示，请重新加载，或下载原件查看。';
  } finally {
    if (current === revision) controls(false);
  }
}

for (const name of ['prev', 'next', 'zoom-out', 'zoom-in', 'fit', 'rotate']) {
  action(name).addEventListener('click', () => {
    if (name === 'prev') page = Math.max(1, page - 1);
    if (name === 'next') page = Math.min(documentPdf.numPages, page + 1);
    if (name === 'zoom-out') zoom = Math.max(.5, zoom - .25);
    if (name === 'zoom-in') zoom = Math.min(3, zoom + .25);
    if (name === 'fit') zoom = 1;
    if (name === 'rotate') rotation = (rotation + 90) % 360;
    viewport.scrollTo(0, 0);
    paint();
  });
}
root.querySelector('#reader-retry').addEventListener('click', () => location.reload());
let resizeTimer;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(paint, 120);
});

try {
  const vendor = new URL('../storage/vendor/pdfjs/', import.meta.url);
  const pdfjs = await import(new URL('pdf.min.mjs', vendor).href);
  pdfjs.GlobalWorkerOptions.workerSrc = new URL('pdf.worker.min.mjs', vendor).href;
  documentPdf = await pdfjs.getDocument({
    url: root.dataset.source, withCredentials: true, isEvalSupported: false,
    cMapUrl: new URL('cmaps/', vendor).href, cMapPacked: true,
    standardFontDataUrl: new URL('standard_fonts/', vendor).href,
    wasmUrl: new URL('wasm/', vendor).href,
  }).promise;
  await paint();
} catch {
  controls(true);
  viewport.hidden = true;
  recovery.hidden = false;
  status.textContent = 'PDF 暂时无法加载，可能已撤下或网络中断。请返回资料页核对，或重新加载。';
}
