import * as pdfjs from "./vendor/pdfjs/pdf.min.mjs";

const vendor = new URL("./vendor/pdfjs/", import.meta.url);
pdfjs.GlobalWorkerOptions.workerSrc = new URL("pdf.worker.min.mjs", vendor).href;
const root = document.getElementById("annotation-editor");
const one = selector => root.querySelector(selector);
const files = JSON.parse(one("#annotation-files").textContent) || [];
const notes = JSON.parse(one("#annotation-data").textContent) || [];
const editable = root.dataset.editable === "true";
const layer = one("#anno-layer"), canvas = one("canvas"), stage = one(".anno-stage"), plane = one(".anno-plane");
const select = one("#anno-file"), status = one("#anno-status"), pages = one("#anno-pages");
const action = name => one('[data-action="' + name + '"]');
let current, info, doc, bitmap, page = 1, turn = 0, zoom = 1, mode = "ink", stroke;
let loadId = 0, paintId = 0;

function sync() {
  if (editable) one("#annotation-value").value = JSON.stringify(notes);
}
function currentNotes() {
  return notes.filter(note => String(note.attachment_id) === String(current.id) && note.page === page);
}
function svgNode(tag, attrs, value) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [name,val] of Object.entries(attrs)) node.setAttribute(name, String(val));
  if (value) node.textContent = value;
  return node;
}
function draw() {
  layer.replaceChildren();
  const list = one("#anno-list");
  list.replaceChildren();
  for (const note of currentNotes()) {
    if (note.type === "ink") {
      const pts = note.points.map(([x,y]) => (x*1000) + "," + (y*1000)).join(" ");
      layer.append(svgNode("polyline", {points:pts,fill:"none",stroke:"#b14227",
        "stroke-width":note.stroke_width*1000,"stroke-linecap":"round","stroke-linejoin":"round"}));
    } else {
      layer.append(svgNode("circle",{cx:note.x*1000,cy:note.y*1000,r:5,fill:"#a12e19"}));
      layer.append(svgNode("text",{x:note.x*1000+6,y:Math.max(18,note.y*1000-5),
        fill:"#9d2916","font-size":21,"font-weight":600},note.text));
    }
    const item = document.createElement("li");
    item.textContent = note.type === "ink" ? "手写批注" : note.text;
    list.append(item);
  }
  if (editable) action("undo").disabled = currentNotes().length === 0;
}
function layout() {
  if (!canvas.width || !canvas.height || !info || info.state !== "ready") return;
  const ratio = canvas.height/canvas.width, sideways = turn%180 !== 0;
  const available = Math.max(150, one(".anno-scroll").clientWidth);
  const width = Math.max(90, Math.min(960, available/(sideways ? ratio : 1)) * zoom), height = width*ratio;
  stage.style.width = (sideways ? height : width) + "px";
  stage.style.height = (sideways ? width : height) + "px";
  plane.style.width = width + "px";
  plane.style.height = height + "px";
  const move = {0:[0,0],90:[height,0],180:[width,height],270:[0,width]}[turn];
  plane.style.transform = "translate(" + move[0] + "px," + move[1] + "px) rotate(" + turn + "deg)";
}
async function paint() {
  const revision = ++paintId;
  const frame = document.createElement("canvas");
  if (doc) {
    const pdfPage = await doc.getPage(page);
    const normal = pdfPage.getViewport({scale:1});
    const scale = Math.min(2, 1900/Math.max(normal.width,normal.height));
    const viewport = pdfPage.getViewport({scale});
    frame.width = Math.max(1,Math.ceil(viewport.width));
    frame.height = Math.max(1,Math.ceil(viewport.height));
    await pdfPage.render({canvasContext:frame.getContext("2d"),viewport}).promise;
  } else if (bitmap) {
    const scale = Math.min(1, 2000/Math.max(bitmap.naturalWidth,bitmap.naturalHeight));
    frame.width = Math.max(1,Math.round(bitmap.naturalWidth*scale));
    frame.height = Math.max(1,Math.round(bitmap.naturalHeight*scale));
    frame.getContext("2d").drawImage(bitmap,0,0,frame.width,frame.height);
  } else return;
  if (revision !== paintId) return;
  canvas.width = frame.width;
  canvas.height = frame.height;
  canvas.getContext("2d").drawImage(frame,0,0);
  one(".anno-scroll").hidden = false;
  pages.textContent = page + " / " + info.pages.length;
  action("prev").disabled = page <= 1;
  action("next").disabled = page >= info.pages.length;
  status.textContent = current.name + " · 第 " + page + " 页";
  layout();
  draw();
}
function showRecovery(message, retry) {
  status.textContent = message;
  one(".anno-scroll").hidden = true;
  one("#anno-recovery").hidden = false;
  if (editable) action("retry").hidden = !retry;
}
async function openFile(retry=false, targetPage=1) {
  const revision = ++loadId;
  ++paintId;
  stroke = null;
  const oldDoc = doc;
  doc = null; bitmap = null; info = null;
  if (oldDoc) oldDoc.destroy().catch(() => {});
  current = files[Number(select.value) || 0];
  pages.textContent = "—";
  action("prev").disabled = action("next").disabled = true;
  one(".anno-scroll").hidden = true;
  one("#anno-list").replaceChildren();
  one("#anno-recovery").hidden = true;
  if (!current) { status.textContent = "此版本没有作答附件。"; return; }
  one("#anno-original").href = current.file_url;
  page = 1; turn = 0; zoom = 1;
  status.textContent = "正在载入 " + current.name + "……";
  try {
    const options = {credentials:"same-origin",cache:"no-store"};
    if (retry) {
      const token = root.closest("form")?.querySelector("[name=csrfmiddlewaretoken]")?.value;
      options.method = "POST";
      options.body = new URLSearchParams({action:"retry",csrfmiddlewaretoken:token || ""});
    }
    const answer = await fetch(current.status_url,options);
    if (!answer.ok) throw Error("status");
    const result = await answer.json();
    if (revision !== loadId) return;
    info = result;
    if (info.state !== "ready") {
      showRecovery(info.state === "failed" ? "预览生成失败，可以下载原件。" : "等待工作进程生成预览，可先下载原件。",info.state === "failed");
      return;
    }
    if (Number.isInteger(targetPage) && targetPage >= 1 && targetPage <= info.pages.length) page = targetPage;
    if (current.kind === "image") {
      const image = new Image(); image.src = current.preview_url; await image.decode();
      if (revision !== loadId) return;
      bitmap = image;
    } else {
      const opened = await pdfjs.getDocument({url:current.preview_url, isEvalSupported:false,
        cMapUrl:new URL("cmaps/",vendor).href,cMapPacked:true,standardFontDataUrl:new URL("standard_fonts/",vendor).href,
        wasmUrl:new URL("wasm/",vendor).href,iccUrl:new URL("iccs/",vendor).href}).promise;
      if (revision !== loadId) { await opened.destroy(); return; }
      doc = opened;
    }
    await paint();
  } catch {
    if (revision === loadId) showRecovery("暂时无法读取此页，请刷新或下载原件。",false);
  }
}
function point(event) {
  const matrix = layer.getScreenCTM();
  if (!matrix) return null;
  const p = new DOMPoint(event.clientX,event.clientY).matrixTransform(matrix.inverse());
  return [p.x,p.y].map(n=>Math.round(Math.max(0,Math.min(1,n/1000))*10000)/10000);
}
function append(type, pos) {
  const dim = info?.pages[page-1];
  if (!editable || !dim || !pos || one(".anno-scroll").hidden) return;
  const note = {type,attachment_id:current.id,source_sha256:current.sha256,renderer_version:info.renderer_version,
    page,width:dim.width,height:dim.height,rotation:dim.rotation,x:pos[0],y:pos[1]};
  if (type === "ink") {
    note.points = [pos]; note.stroke_width = .004;
  } else {
    note.text = one("#anno-text").value.trim();
    if (!note.text) { one("#anno-text").focus(); return; }
  }
  notes.push(note); sync(); draw();
  return note;
}
layer.addEventListener("pointerdown",event => {
  if (!editable || event.button !== 0 || stroke) return;
  event.preventDefault();
  if (mode === "ink") {
    const note = append("ink", point(event));
    if (note) { stroke = {id:event.pointerId,note}; layer.setPointerCapture(event.pointerId); }
  } else append("text",point(event));
});
layer.addEventListener("pointermove",event => {
  if (!stroke || stroke.id !== event.pointerId || stroke.note.points.length >= 2000) return;
  const pos = point(event);
  if (pos) { stroke.note.points.push(pos); draw(); }
});
function endStroke(event) {
  if (stroke?.id !== event.pointerId) return;
  if (stroke.note.points.length === 1) stroke.note.points.push(stroke.note.points[0]);
  stroke = null; sync(); draw();
}
layer.addEventListener("pointerup",endStroke);
layer.addEventListener("pointercancel",endStroke);
root.closest("form")?.addEventListener("submit",sync);
root.addEventListener("click",async e=>{
  const button = e.target.closest("button[data-action]");
  if (!button) return;
  const cmd = button.dataset.action;
  if (cmd === "refresh" || cmd === "retry") { await openFile(cmd === "retry"); return; }
  if (cmd === "prev" || cmd === "next") {
    page += cmd === "next" ? 1 : -1;
    try { await paint(); } catch { showRecovery("此页暂时无法显示，可重试。",false); }
  } else if (cmd === "rotate") { turn=(turn+90)%360; layout(); }
  else if (cmd === "zoom-in" || cmd === "zoom-out") { zoom=Math.max(.6,Math.min(2.5,zoom+(cmd==="zoom-in"?.25:-.25))); layout(); }
  else if (cmd === "ink" || cmd === "text") {
    mode=cmd;
    action("ink").setAttribute("aria-pressed",String(cmd==="ink"));
    action("text").setAttribute("aria-pressed",String(cmd==="text"));
    one("#anno-textbox").hidden = cmd !== "text";
    if (cmd === "text") one("#anno-text").focus();
  } else if (cmd === "center") append("text",[.5,.5]);
  else if (cmd === "undo" && current) {
    const last = currentNotes().at(-1);
    if (last) { notes.splice(notes.indexOf(last),1); sync(); draw(); }
  }
});
files.forEach((file,i) => select.add(new Option((i+1) + " · " + file.name,String(i))));
select.addEventListener("change",()=>openFile());
new ResizeObserver(layout).observe(one(".anno-scroll"));
const params = new URLSearchParams(location.search);
const pinned = files.findIndex(file => String(file.id) === params.get("attachment"));
if (pinned >= 0) select.value = String(pinned);
sync(); openFile(false, pinned >= 0 ? Number(params.get("page")) : 1);
