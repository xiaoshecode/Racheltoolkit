/* Conversation state is page-local; only explicitly confirmed text is sent. */
(() => {
  'use strict';
  const root = document.querySelector('[data-chat-root]');
  const form = root?.querySelector('[data-chat-form]');
  if (!form) return;
  const find = name => root.querySelector(`[data-chat-${name}]`);
  const input = document.getElementById('chat-question');
  const transcript = find('transcript');
  const welcome = find('welcome');
  const consent = find('consent');
  const thinking = find('thinking');
  const send = find('send');
  const stop = find('stop');
  const retry = find('retry');
  const errorBox = find('error');
  const status = find('status');
  const jump = find('jump');
  const fileInput = find('files');
  const attachmentList = find('attachments');
  let selectedFiles = [];
  let readyAttachments = [];
  let uploading = false;
  let history = [];
  let controller = null;
  let pending = null;
  let following = true;
  let frame = 0;

  const count = text => [...text].length;
  const nearBottom = () => transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 80;
  function refresh() {
    const busy = Boolean(controller) || uploading;
    send.disabled = busy || !consent.checked || !input.value.trim() || count(input.value) > 4000;
    input.disabled = busy;
    thinking.disabled = busy;
    consent.disabled = busy;
    stop.hidden = !controller || uploading;
    if (fileInput) fileInput.disabled = busy;
    find('new').disabled = busy;
    retry.disabled = busy || !consent.checked;
    attachmentList?.querySelectorAll('button').forEach(button => { button.disabled = busy; });
    root.querySelectorAll('[data-chat-prompt]').forEach(button => { button.disabled = busy; });
  }
  function scroll() {
    if (following) transcript.scrollTop = transcript.scrollHeight;
    jump.hidden = following || nearBottom();
  }
  transcript.addEventListener('scroll', () => { following = nearBottom(); jump.hidden = following; });
  jump.addEventListener('click', () => { following = true; scroll(); });
  function error(message, canRetry = false) {
    find('error-text').textContent = message;
    errorBox.hidden = false;
    retry.hidden = !canRetry;
  }
  function clearError() { errorBox.hidden = true; retry.hidden = true; }
  const entityDecoder = document.createElement('textarea');
  function decodeEntities(value) {
    return String(value).replace(/&(?:#\d+|#x[\da-f]+|[a-z][a-z\d]+);/gi, entity => {
      // Only an individual entity enters the inert decoder, never model HTML.
      entityDecoder.innerHTML = entity;
      return entityDecoder.value;
    });
  }
  function safeURL(value) {
    const href = decodeEntities(value || '');
    if (!href || /[\u0000-\u0020\u007f]/.test(href)) return null;
    try {
      const url = new URL(href, window.location.href);
      if (!['https:', 'http:', 'mailto:'].includes(url.protocol) || url.username || url.password) return null;
      return url.href;
    } catch { return null; }
  }
  function renderTokens(parent, tokens, depth = 0, inLink = false) {
    for (const token of tokens || []) {
      if (depth > 40) { parent.append(document.createTextNode(token.raw || token.text || '')); continue; }
      const element = tag => document.createElement(tag);
      const children = (node, nested = token.tokens, linked = inLink) => renderTokens(node, nested, depth + 1, linked);
      let node;
      switch (token.type) {
        case 'space': case 'def': continue;
        case 'text':
          if (token.tokens) children(parent);
          else parent.append(document.createTextNode(decodeEntities(token.text || '')));
          continue;
        case 'escape': case 'html':
          parent.append(document.createTextNode(token.text || token.raw || '')); continue;
        case 'paragraph': node = element('p'); children(node); break;
        case 'heading': node = element(`h${Math.min(6, Math.max(2, token.depth + 1))}`); children(node); break;
        case 'strong': case 'em': case 'del': node = element(token.type); children(node); break;
        case 'br': node = element('br'); break;
        case 'hr': node = element('hr'); break;
        case 'codespan': node = element('code'); node.textContent = token.text; break;
        case 'code': {
          node = element('div'); node.className = 'ai-markdown-code';
          const label = element('div'); label.className = 'ai-markdown-code-label';
          label.textContent = token.lang?.split(/\s/)[0] || '代码';
          const pre = element('pre'); const code = element('code');
          code.textContent = token.text; pre.append(code); node.append(label, pre); break;
        }
        case 'blockquote': node = element('blockquote'); children(node); break;
        case 'list':
          node = element(token.ordered ? 'ol' : 'ul');
          if (token.ordered && Number.isInteger(token.start)) node.start = token.start;
          for (const item of token.items || []) {
            const li = element('li');
            if (item.task) li.className = 'ai-markdown-task';
            children(li, item.tokens); node.append(li);
          }
          break;
        case 'checkbox':
          node = element('input'); node.type = 'checkbox'; node.checked = Boolean(token.checked);
          node.disabled = true; node.setAttribute('aria-label', token.checked ? '已完成' : '未完成'); break;
        case 'table': {
          node = element('div'); node.className = 'ai-markdown-table'; node.tabIndex = 0;
          node.setAttribute('role', 'region'); node.setAttribute('aria-label', '回复表格，可横向滚动');
          const table = element('table'); const head = element('thead'); const body = element('tbody');
          function row(cells, header) {
            const tr = element('tr');
            cells.forEach((cell, index) => {
              const td = element(header ? 'th' : 'td');
              if (header) td.scope = 'col';
              const alignment = cell.align || token.align?.[index];
              if (['left','center','right'].includes(alignment)) td.style.textAlign = alignment;
              children(td, cell.tokens); tr.append(td);
            });
            return tr;
          }
          head.append(row(token.header || [], true));
          (token.rows || []).forEach(cells => body.append(row(cells, false)));
          table.append(head, body); node.append(table); break;
        }
        case 'link': case 'image': {
          const href = safeURL(token.href);
          node = element(href && !inLink ? 'a' : 'span');
          if (node.tagName === 'A') {
            node.href = href; node.target = '_blank'; node.rel = 'noopener noreferrer';
            node.referrerPolicy = 'no-referrer';
            if (token.title) node.title = token.title;
          }
          if (token.type === 'image') node.textContent = `图片：${decodeEntities(token.text || '查看图片')}`;
          else children(node, token.tokens, true);
          break;
        }
        default: parent.append(document.createTextNode(token.raw || token.text || '')); continue;
      }
      parent.append(node);
    }
  }
  function renderText(container, text, markdown = false) {
    container.classList.toggle('ai-markdown', markdown);
    if (!markdown || !window.marked?.lexer) { container.textContent = text; return; }
    try {
      const fragment = document.createDocumentFragment();
      renderTokens(fragment, window.marked.lexer(text, {gfm: true, breaks: true}));
      container.replaceChildren(fragment);
    } catch {
      container.classList.remove('ai-markdown');
      container.textContent = text;
    }
  }
  function message(role, text = '') {
    welcome.hidden = true;
    const article = document.createElement('article');
    article.className = `ai-chat-message ai-chat-${role}`;
    const label = document.createElement('p');
    label.className = 'ai-chat-role';
    label.textContent = role === 'user' ? '你' : '学习助手';
    const body = document.createElement('div');
    body.className = 'ai-chat-body';
    article.append(label, body);
    let details, reasoning, copy, toggle, model;
    if (role === 'assistant') {
      details = document.createElement('details');
      details.className = 'ai-chat-reasoning';
      details.hidden = true;
      const summary = document.createElement('summary');
      summary.textContent = '查看思考过程';
      reasoning = document.createElement('div');
      details.append(summary, reasoning);
      article.insertBefore(details, body);
      copy = document.createElement('button');
      copy.type = 'button';
      copy.className = 'quiet ai-chat-copy';
      copy.textContent = '复制回复';
      copy.hidden = true;
      const actions = document.createElement('div');
      actions.className = 'ai-chat-reply-actions';
      toggle = document.createElement('button');
      toggle.type = 'button';
      toggle.className = 'quiet ai-chat-copy';
      toggle.textContent = 'Markdown 原文';
      toggle.setAttribute('aria-pressed', 'false');
      model = document.createElement('span');
      model.className = 'ai-chat-model';
      actions.append(toggle, copy, model);
      article.append(actions);
    }
    renderText(body, text, role === 'assistant');
    transcript.append(article);
    scroll();
    return {article, body, details, reasoning, copy, toggle, model};
  }
  function boundedHistory(question, attachments = []) {
    const current = {role: 'user', content: question};
    if (attachments.length) current.attachments = attachments;
    const result = [...history, current];
    let removed = false;
    while (result.length > 19 || result.reduce((sum, entry) => sum + count(entry.content), 0) > 24000) {
      if (result.length <= 1) throw new Error('问题过长，请缩短后重试。');
      result.splice(0, 2);
      removed = true;
    }
    return {result, removed};
  }
  async function consume(response, onEvent, signal) {
    if (!response.body) throw new Error('浏览器不支持流式回复，请更新浏览器后再试。');
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    const process = () => {
      let end;
      while ((end = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, end).replace(/\r$/, '');
        buffer = buffer.slice(end + 1);
        if (line.startsWith('data: ')) onEvent(JSON.parse(line.slice(6)));
      }
      if (buffer.length > 262144) throw new Error('回复格式异常，请重试。');
    };
    try {
      while (true) {
        if (signal.aborted) throw new DOMException('Stopped', 'AbortError');
        const {done, value} = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, {stream: true});
        process();
      }
      buffer += decoder.decode();
      if (buffer) { buffer += '\n'; process(); }
    } finally {
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }
  async function ask(question, reuse = null, attachments = []) {
    if (controller || !consent.checked) return;
    question = question.trim();
    if (!question || count(question) > 4000) return;
    clearError();
    if (reuse) attachments = reuse.attachments;
    const {result, removed} = boundedHistory(question, attachments);
    const user = reuse?.user || message('user', question);
    const assistant = reuse?.assistant || message('assistant');
    pending = {question, user, assistant, attachments};
    if (attachments.length && !reuse) {
      const attached = document.createElement('p');
      attached.className = 'ai-chat-attached-names';
      attached.textContent = '附件：' + readyAttachments.map(item => item.name).join('、');
      user.article.append(attached);
    }
    let answer = '';
    let reasoning = '';
    let finish = null;
    let sourceMode = false;
    assistant.toggle.setAttribute('aria-pressed', 'false');
    assistant.toggle.textContent = 'Markdown 原文';
    assistant.model.textContent = '';
    assistant.article.classList.remove('is-incomplete');
    assistant.copy.hidden = true;
    assistant.details.hidden = true;
    assistant.reasoning.textContent = '';
    assistant.body.textContent = '正在准备回复……';
    const active = new AbortController();
    controller = active;
    refresh();
    status.textContent = removed ? '较早的对话已移出本轮上下文，正在生成回复……' : '正在生成回复……';
    const paint = () => {
      frame = 0;
      renderText(assistant.body, answer || (reasoning ? '正在思考……' : '正在准备回复……'), !sourceMode);
      renderText(assistant.reasoning, reasoning, !sourceMode);
      assistant.details.hidden = !reasoning;
      scroll();
    };
    assistant.toggle.onclick = () => {
      if (frame) { clearTimeout(frame); frame = 0; }
      sourceMode = !sourceMode;
      assistant.toggle.setAttribute('aria-pressed', String(sourceMode));
      assistant.toggle.textContent = sourceMode ? '预览回复' : 'Markdown 原文';
      paint();
    };
    try {
      const response = await fetch(form.dataset.chatEndpoint, {
        method: 'POST', credentials: 'same-origin', signal: active.signal,
        headers: {'Content-Type':'application/json', 'X-CSRFToken':form.querySelector('[name=csrfmiddlewaretoken]').value},
        body: JSON.stringify({messages:result, thinking:thinking.checked, consent:true}),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.error || (response.status === 403 ? '登录状态或发送权限已改变，请刷新页面后再试。' : '暂时无法生成回复，请稍后重试。'));
      }
      if (!response.headers.get('content-type')?.includes('text/event-stream')) throw new Error('登录状态已改变，请刷新后重新登录。');
      await consume(response, item => {
        if (item.type === 'error') throw new Error(item.message || '回复中断，请重试。');
        if (item.type === 'model') {
          assistant.model.textContent = `${item.model} · ${item.reason || ''}`;
        } else if (item.type === 'content') answer += item.content;
        else if (item.type === 'reasoning') reasoning += item.content;
        else if (item.type === 'done') finish = item.finish_reason;
        if (count(answer) + count(reasoning) > 120000) throw new Error('回复过长，请拆分问题。');
        if (!frame) frame = setTimeout(paint, 100);
      }, active.signal);
      if (active.signal.aborted) throw new DOMException('Stopped', 'AbortError');
      if (!['stop','length'].includes(finish) || !answer) throw new Error('回复未完成，请重试当前问题。');
      history = result.slice(0,-1);
      if (count(answer) <= 16000) history.push(result.at(-1), {role:'assistant',content:answer});

      input.value = '';
      pending = null;
      assistant.copy.hidden = false;
      assistant.copy.onclick = async () => {
        try { await navigator.clipboard.writeText(answer); assistant.copy.textContent = '已复制'; }
        catch { status.textContent = '无法自动复制，请选中回复文字复制。'; }
      };
      status.textContent = count(answer) > 16000 ? '回复较长，本次内容不会加入后续对话。' :
        (finish === 'length' ? '回复达到长度上限，可继续提问补充。' : '回复已完成。');
    } catch (failure) {
      assistant.article.classList.add('is-incomplete');
      if (failure.name === 'AbortError') {
        status.textContent = '已停止生成，本次回复不会加入后续对话。';
        error('可以重新发送这个问题。', true);
      } else {
        status.textContent = '回复未完成。';
        error(failure.message || '网络连接中断，请重试。', true);
      }
    } finally {
      if (frame) { clearTimeout(frame); frame = 0; }
      paint();
      controller = null;
      refresh();
    }
  }
  function drawAttachments() {
    if (!attachmentList) return;
    attachmentList.replaceChildren();
    const items = [...readyAttachments.map(item => ({...item, ready:true})),
      ...selectedFiles.map((file, index) => ({name:file.name, size:file.size, index}))];
    items.forEach(item => {
      const chip = document.createElement('span');
      chip.className = 'ai-chat-attachment';
      const name = document.createElement('span');
      name.textContent = `${item.name} · ${(item.size / 1024).toFixed(0)}KB${item.ready ? ' · 已上传' : ''}`;
      const remove = document.createElement('button');
      remove.type = 'button'; remove.className = 'quiet'; remove.textContent = '×';
      remove.setAttribute('aria-label', `移除 ${item.name}`);
      remove.disabled = Boolean(controller) || uploading;
      remove.onclick = () => {
        if (controller || uploading) return;
        if (item.ready) readyAttachments = readyAttachments.filter(value => value.id !== item.id);
        else selectedFiles.splice(item.index,1);
        drawAttachments(); refresh();
      };
      chip.append(name, remove); attachmentList.append(chip);
    });
  }
  fileInput?.addEventListener('change', () => {
    const added = [...fileInput.files];
    fileInput.value = '';
    const proposed = [...selectedFiles, ...added];
    if (proposed.length + readyAttachments.length > 3 ||
        proposed.reduce((sum, file) => sum + file.size,0) + readyAttachments.reduce((sum,item)=>sum+item.size,0) > 20*1024*1024) {
      error('每轮最多选择 3 个文件，合计不超过 20MB。'); return;
    }
    selectedFiles = proposed;
    clearError(); drawAttachments();
  });
  async function sendQuestion() {
    if (controller || uploading || !form.reportValidity()) return;
    const question = input.value;
    if (selectedFiles.length) {
      clearError(); uploading = true; refresh(); drawAttachments();
      status.textContent = '正在上传并解析附件，请稍候。解析完成前不会发送问题。';
      try {
        const body = new FormData(); body.append('consent','true');
        selectedFiles.forEach(file => body.append('files',file));
        const response = await fetch(form.dataset.chatUploadEndpoint, {
          method:'POST', credentials:'same-origin',
          headers:{'X-CSRFToken':form.querySelector('[name=csrfmiddlewaretoken]').value}, body,
        });
        const result = await response.json().catch(() => ({}));
        const received = Array.isArray(result.attachments) ? result.attachments : [];
        for (const item of received) {
          if (!readyAttachments.some(existing=>existing.id===item.id)) readyAttachments.push(item);
        }
        // Successful Files uploads are returned in selection order, even on partial failure.
        selectedFiles = selectedFiles.slice(received.length);
        if (!response.ok) throw new Error(result.error || '附件上传未完成，请检查文件后重新发送。');
        if (selectedFiles.length) throw new Error('附件未全部处理完成，请稍后重新发送。');
      } catch (failure) {
        error(failure.message || '附件上传连接中断，请检查后再试。');
        status.textContent = '附件上传未完成，问题尚未发送。';
        return;
      } finally { uploading = false; refresh(); drawAttachments(); }
    }
    const attachments = readyAttachments.map(item=>item.id);
    await ask(question, null, attachments);
    selectedFiles = []; readyAttachments = []; drawAttachments();
  }
  form.addEventListener('submit', event => { event.preventDefault(); sendQuestion(); });
  input.addEventListener('input', refresh);
  consent.addEventListener('change', refresh);
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault();
      if (!send.disabled) form.requestSubmit();
    }
  });
  stop.addEventListener('click', () => controller?.abort());
  retry.addEventListener('click', () => { if (pending && consent.checked) ask(pending.question, pending); });
  find('new').addEventListener('click', () => {
    if (controller) return;
    history = [];
    pending = null;
    selectedFiles = []; readyAttachments = []; drawAttachments();
    transcript.querySelectorAll('.ai-chat-message').forEach(element => element.remove());
    welcome.hidden = false;
    input.value = '';
    following = true;
    status.textContent = '已开启新对话。';
    clearError();
    refresh();
    input.focus();
  });
  root.querySelectorAll('[data-chat-prompt]').forEach(button => button.addEventListener('click', () => {
    input.value = button.dataset.chatPrompt;
    refresh();
    input.focus();
  }));
  refresh();
})();
