const form = document.querySelector('#submission-form');
const queue = document.querySelector('#queue'), message = document.querySelector('#receipt-status');
const owner = document.querySelector('#owner');
let items = [], key = newKey(), busy = false;
function newKey() { return crypto.randomUUID(); }
function resetRequest() { key = newKey(); message.textContent = ''; }
function redraw() {
  queue.replaceChildren();
  items.forEach((item, i) => {
    const row = document.createElement('li');
    if (item.url) {
      const img = new Image(); img.src = item.url; img.height = 72; img.alt = '待交第 ' + (i+1) + ' 页';
      row.append(img);
    }
    row.append(document.createTextNode(' ' + item.file.name + ' '));
    for (const [label, offset] of [['上移',-1],['下移',1],['移除',0]]) {
      const btn = document.createElement('button'); btn.type='button'; btn.textContent=label;
      btn.disabled=busy || (offset !== 0 && (i+offset < 0 || i+offset >= items.length));
      btn.onclick=() => {
        if (!offset) { URL.revokeObjectURL(item.url || ''); items.splice(i,1); }
        else [items[i],items[i+offset]]=[items[i+offset],items[i]];
        resetRequest(); redraw();
      }; row.append(btn);
    }
    queue.append(row);
  });
}
for (const id of ['files','camera']) {
  document.getElementById(id).addEventListener('change',e=>{
    for (const file of e.target.files) items.push({file,url:/^image\/(jpeg|png)$/.test(file.type)?URL.createObjectURL(file):null});
    e.target.value=''; resetRequest(); redraw();
  });
}
if(owner) owner.addEventListener('change',resetRequest);
form.elements.note?.addEventListener('input',resetRequest);
form.addEventListener('submit',async e=>{
  e.preventDefault(); if(busy)return;
  if(!items.length){message.textContent='请先选择文件。';return;}
  const url=owner ? '/teacher/assignments/'+form.dataset.work+'/students/'+owner.value+'/submissions/' : form.action;
  const data=new FormData(form); for(const item of items)data.append('files',item.file);
  data.set('key',key);
  message.textContent='正在核验整版文件…'; busy=true;
  const controls=[...form.elements]; controls.forEach(el=>el.disabled=true); redraw();
  let received=false;
  try {
    const response=await fetch(url,{method:'POST',body:data,credentials:'same-origin'});
    const result=await response.json();
    if(result.receipt) {received=true; location.assign(result.url);}
    else message.textContent=result.error || '尚未收到成功回执。请保留文件并再次提交，系统会核对同一个版本。';
  } catch {message.textContent='网络未确认收件。请保留文件再次提交，复用本次提交编号。';}
  finally {if(!received){busy=false;controls.forEach(el=>el.disabled=false);redraw();}}
});

