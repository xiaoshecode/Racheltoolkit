const form = document.querySelector('#submission-form');
const queue = document.querySelector('#queue'), message = document.querySelector('#receipt-status');
const owner = document.querySelector('#owner');
const fresh = document.querySelector('#fresh-version');
const submitButton = document.querySelector('#submit-version');
const queueSummary = document.querySelector('#queue-summary');
const queueEmpty = document.querySelector('#queue-empty');
let items = [], key = newKey(), busy = false, submitted = false;
function newKey() { return crypto.randomUUID(); }
function fileSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KiB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MiB';
}
function submitState() {
  form.setAttribute('aria-busy', String(busy));
  submitButton.textContent = busy ? '正在提交整版…' : submitted ? '使用原编号重试整版' : '确认并提交整版';
}
function cacheName() {
  return ['teaching-intake',form.dataset.actor,form.dataset.work,owner?.value || 'self'].join(':');
}
function remember(value) {
  try {
    if (value) sessionStorage.setItem(cacheName(),value);
    else sessionStorage.removeItem(cacheName());
  } catch { /* within-page retries still use the active key */ }
}
function resetRequest() {
  if (!submitted) key = newKey();
  message.textContent = submitted ? '正在重试原版。如需更换清单，请先另起一版。' : '';
  submitState();
}
async function recover() {
  let saved=null;
  try { saved=sessionStorage.getItem(cacheName()); } catch { }
  key=saved || newKey(); submitted=!!saved; fresh.hidden=!submitted;
  submitState();
  if (!saved) { message.textContent=''; return; }
  message.textContent='正在核对上次提交…';
  const route=owner ? '/teacher/assignments/'+form.dataset.work+'/students/'+owner.value : '/assignments/'+form.dataset.work;
  try {
    const res=await fetch(route+'/submission-status/?key='+encodeURIComponent(saved),{cache:'no-store'});
    if (key!==saved) return;
    if(res.status===200 && !res.redirected){
      const data=await res.json();
      if(data.receipt){remember(null);location.assign(data.url);return;}
    }
    message.textContent='上次提交尚未确认。重新选取相同文件并按原页序提交，或另起一版。';
  } catch { if(key===saved)message.textContent='暂时无法查询回执。可使用原文件、原页序重试。'; }
}
fresh.onclick=()=>{remember(null);submitted=false;fresh.hidden=true;resetRequest();};
function redraw() {
  queue.replaceChildren();
  queueEmpty.hidden = items.length > 0;
  queueSummary.textContent = (items.length ? items.length + ' 个文件' : '尚未选择文件') + ' · ' + fileSize(items.reduce((total, item) => total + item.file.size, 0)) + ' / 20 MiB';
  submitState();
  if (owner) {
    const option = owner.selectedOptions[0];
    document.querySelector('#proxy-target').textContent = owner.value
      ? option.textContent+' · '+form.dataset.title+' · 上次成功 V'+option.dataset.latest+'，本次代交 '+items.length+' 个附件。版本以整版回执为准。'
      : '请先选择所属学生。';
  }
  items.forEach((item, i) => {
    const row = document.createElement('li');
    if (item.url) {
      const img = new Image(); img.src = item.url; img.className = 'queue-preview'; img.alt = '待交第 ' + (i+1) + ' 页';
      row.append(img);
    } else {
      const preview = document.createElement('span'); preview.className = 'queue-preview queue-document';
      preview.textContent = item.file.name.split('.').pop().slice(0, 8).toUpperCase();
      preview.setAttribute('aria-hidden', 'true'); row.append(preview);
    }
    const details = document.createElement('div'); details.className = 'queue-file';
    const position = document.createElement('span'); position.className = 'queue-position';
    position.textContent = item.url ? '第 ' + (i+1) + ' 页' : '附件 ' + (i+1);
    const filename = document.createElement('strong'); filename.textContent = item.file.name;
    const size = document.createElement('span'); size.textContent = fileSize(item.file.size);
    details.append(position, filename, size); row.append(details);
    const actions = document.createElement('div'); actions.className = 'queue-actions';
    for (const [label, offset] of [['上移',-1],['下移',1],['移除',0]]) {
      const btn = document.createElement('button'); btn.type='button'; btn.textContent=label;
      btn.setAttribute('aria-label', label + '第 ' + (i+1) + ' 项：' + item.file.name);
      btn.disabled=busy || (offset !== 0 && (i+offset < 0 || i+offset >= items.length));
      btn.onclick=() => {
        if (!offset) { URL.revokeObjectURL(item.url || ''); items.splice(i,1); }
        else [items[i],items[i+offset]]=[items[i+offset],items[i]];
        resetRequest(); redraw();
      }; actions.append(btn);
    }
    row.append(actions);
    queue.append(row);
  });
}
for (const id of ['files','camera']) {
  document.getElementById(id).addEventListener('change',e=>{
    for (const file of e.target.files) items.push({file,url:/^image\/(jpeg|png)$/.test(file.type)?URL.createObjectURL(file):null});
    e.target.value=''; resetRequest(); redraw();
  });
}
if(owner) owner.addEventListener('change',()=>{
  for (const item of items) if (item.url) URL.revokeObjectURL(item.url);
  items=[];
  form.elements.note.value='';
  redraw();
  recover();
});
redraw();
recover();
form.elements.note?.addEventListener('input',resetRequest);
form.addEventListener('submit',async e=>{
  e.preventDefault(); if(busy)return;
  if(!items.length){message.textContent='请先选择文件。';return;}
  const url=owner ? '/teacher/assignments/'+form.dataset.work+'/students/'+owner.value+'/submissions/' : form.action;
  const data=new FormData(form); for(const item of items)data.append('files',item.file);
  data.set('key',key);
  submitted=true; remember(key); fresh.hidden=false;
  message.textContent='正在核验整版文件…'; busy=true;
  const controls=[...form.elements]; controls.forEach(el=>el.disabled=true); redraw();
  let received=false;
  try {
    const response=await fetch(url,{method:'POST',body:data,credentials:'same-origin'});
    const result=await response.json();
    if(result.receipt) {received=true; remember(null); location.assign(result.url);}
    else message.textContent=result.error || '尚未收到成功回执。请保留文件并再次提交，系统会核对同一个版本。';
  } catch {message.textContent='网络未确认收件。请保留文件再次提交，复用本次提交编号。';}
  finally {if(!received){busy=false;controls.forEach(el=>el.disabled=false);redraw();}}
});

