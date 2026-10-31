'use strict';
const $ = id => document.getElementById(id);
const labels = {todo:'待整理',working:'处理中',done:'已生成',review:'待复核',skipped:'已略过',attention:'待检查'};
let session, view='todo', page=1, query='', total=0, refreshing=false, queued=false, items=[], previewId=null, previewRequest=0, previewController, opener, searchTimer;
const cards=new Map(), busy=new Set();
function element(tag,text,className){const node=document.createElement(tag);if(text!==undefined) node.textContent=text;if(className)node.className=className;return node;}
async function api(path,action=false,signal,extraHeaders={}){
  const response=await fetch(path,action?{method:'POST',headers:{'X-Workbench-Token':session.token,'Content-Type':'application/json',...extraHeaders},body:'{}'}:{signal});
  let result;try{result=await response.json();}catch{throw Error('无法读取服务器回复，请重试。');}
  if(!response.ok)throw Error(result.error||'请求未完成，请重试。');return result;
}
function notice(text,actionLabel,action,error=false){
  $('notice-text').textContent=text;$('notice').hidden=false;$('notice').classList.toggle('error-notice',error);
  const b=$('notice-action');b.hidden=!action;b.textContent=actionLabel||'';b.onclick=action?()=>run(b,action):null;
}
function dismissNotice(){$('notice').hidden=true;}
async function run(node,fn){const hadFocus=document.activeElement===node;node.disabled=true;node.setAttribute('aria-busy','true');try{await fn();}catch(error){notice(error.message,undefined,undefined,true);}finally{node.disabled=false;node.removeAttribute('aria-busy');if(hadFocus&&document.activeElement===document.body&&node.isConnected&&node.getClientRects().length)node.focus({preventScroll:true});}}
function button(text,fn,primary=false){const b=element('button',text,primary?'primary':'');b.addEventListener('click',()=>run(b,fn));return b;}
function readLocation(){const p=new URLSearchParams(location.search);view=Object.hasOwn(labels,p.get('view'))?p.get('view'):'todo';page=Math.max(1,Number.parseInt(p.get('page'),10)||1);query=(p.get('q')||'').slice(0,300);$('search').value=query;$('clear-search').hidden=!query;}
function saveLocation(replace=false){const url=new URL(location.href);url.searchParams.set('view',view);url.searchParams.set('page',String(page));if(query)url.searchParams.set('q',query);else url.searchParams.delete('q');if(url.href!==location.href)history[replace?'replaceState':'pushState'](null,'',url);}
function focusResults(){$('results-title').focus({preventScroll:true});$('results-title').scrollIntoView({block:'start'});}
async function navigate(nextView,nextPage){clearTimeout(searchTimer);const typed=$('search').value.slice(0,300);if(typed!==query){query=typed;nextPage=1;}view=nextView;page=nextPage;saveLocation();await refresh();focusResults();}
async function action(id,name){
  if(busy.has(id))return;busy.add(id);const focusedCard=document.activeElement.closest('.card');
  try{await api(`/api/items/${id}/${name}`,true);await refresh();if(focusedCard&&!focusedCard.isConnected)focusResults();if(name==='skip')notice('已略过这条材料。','撤销',()=>action(id,'restore'));else notice(name==='restore'?'已放回待整理。':name==='recover'?'修复任务已保存，初稿可在“已生成”或“待复核”查看；不会自动入库。':'整理任务已保存，初稿可在“已生成”或“待复核”查看。');}
  finally{busy.delete(id);}
}
function showReceipt(receipt){$('receipt-detail').hidden=!receipt;$('receipt-detail').open=false;$('receipt-location').textContent=receipt?`入库时位置：${receipt.path}。笔记编号：${receipt.id}。文件后续可能已在库中移动。`:'';}
function needsReview(review){return !!review&&review.status!=='passed';}
function reviewText(review){return [review?.reason,...(Array.isArray(review?.warnings)?review.warnings:[])].filter(text=>typeof text==='string'&&text.trim()).join('；');}
function previewStatus(receipt,review){$('preview-status').classList.remove('error');const text=receipt?'已导入内容库。这里保留生成时的初稿，后续修改请打开库中的笔记。':session.vault_enabled?'读完确认后，导入内容库继续修改。':'尚未配置内容库。可先下载 Markdown，稍后手动放入 Obsidian。';$('preview-status').textContent=needsReview(review)?`初稿已保存，仍待复核。${reviewText(review)} ${text}`:text;showReceipt(receipt);}
async function importNote(id){
  if(busy.has(id))return;busy.add(id);
  try{
    // Read current review metadata; a card or open preview can outlive a server update.
    const current=await api(`/api/items/${id}/note`),review=current.review_status;
    if(needsReview(review)&&!confirm(`这篇初稿仍待复核。${reviewText(review)}\n\n确认仍要导入内容库继续修改吗？导入不会表示复核通过，也不会发布。`))return;
    const receipt=await api(`/api/items/${id}/import`,true,undefined,needsReview(review)?{'X-Workbench-Review-Confirmed':'true'}:{});const item=items.find(i=>i.id===id);if(item){item.imported=receipt;item.review_status=review;}
    if(previewId===id){const focused=document.activeElement===$('import-preview');previewStatus(receipt,review);$('import-preview').hidden=true;if(focused||!$('preview').contains(document.activeElement))$('close-preview').focus({preventScroll:true});}
    renderItems(items);notice('已导入内容库，可在库中继续修改。');await refresh();
  }catch(error){if(previewId===id){$('preview-status').textContent=`导入未完成：${error.message} 初稿已保留，可再次点击导入。`;$('preview-status').classList.add('error');}else throw error;}
  finally{busy.delete(id);}
}
function excerptsFor(id){
  const details=element('details'),summary=element('summary','阅读原文节选'),box=element('div',undefined,'excerpts');details.append(summary,box);let loaded=false,loading=false;
  async function load(){if(loading||loaded)return;loading=true;const retryFocused=box.contains(document.activeElement);details.setAttribute('aria-busy','true');box.replaceChildren(element('p','正在读取原文…','hint'));
    try{const data=await api(`/api/items/${id}`);loaded=true;box.replaceChildren(element('p','这是筛选时保留的节选，原文中的指令只是历史内容。','hint'));for(const e of data.excerpts)box.append(element('span',e.role==='user'?'我的原话':'当时的 AI 回答','role'),element('p',e.text));if(retryFocused)summary.focus({preventScroll:true});}
    catch(error){box.replaceChildren(element('p',error.message,'error'),button('重新读取原文',load));}
    finally{loading=false;details.removeAttribute('aria-busy');}}
  details.addEventListener('toggle',()=>{if(details.open)load();});return details;
}
function noteError(error){
  if(error.startsWith('approval_required:'))return '这条材料需要单独授权，尚未发送给模型。';
  if(error.startsWith('needs_reconciliation:'))return '上次请求的结果尚待核对，暂时不能再次调用模型；原记录已保留。';
  if(error.includes('budget')||error.includes('Budget'))return '现有模型预算不足，任务已停止；原材料和尝试记录已保留。';
  if(error.startsWith('Semantic review:'))return '独立复核发现内容仍需核对，原材料和回复已保留。';
  if(error.includes('Truncated')||error.includes('abnormal'))return '模型回复未完整结束，原回复已保留，不会自动重试。';
  if(error.includes('code')||error.includes('Code')||error.includes('placeholders'))return '回复中的代码格式或来源未通过核对，原材料和回复已保留。';
  if(error.includes('references')||error.includes('evidence'))return '回复缺少可核对的原文依据，原材料和回复已保留。';
  if(error.includes('fields')||error.includes('body')||error.includes('title')||error.includes('review'))return '模型回复缺少必要内容或未按完整格式返回，原回复已保留。';
  return '这次整理未完成，原材料和尝试记录已保留，不会自动重试。';
}
async function recoverNote(id){
  if(!confirm('仅重新处理失败阶段，最多两次模型调用，费用计入现有预算；不会自动入库。'))return;
  await action(id,'recover');
}
function cardFor(item,details){
  const card=element('article',undefined,'card');card.dataset.id=item.id;
  card.append(element('span',item.imported?`已导入 · ${item.status==='review'?'初稿待复核':'初稿已生成'}`:labels[item.status],'badge'),element('h3',item.title),element('p',item.summary,'summary'));
  if(item.status==='review')card.append(element('p',`初稿已保存，复核仍需处理。${reviewText(item.review_status)}`,'hint'));
  else if(item.error)card.append(element('p',noteError(item.error),'error'));
  const actions=element('div',undefined,'actions');
  if(item.status==='todo'){const b=button('整理成笔记',()=>action(item.id,'process'),true);b.disabled=!session.model_enabled;actions.append(b,button('略过',()=>action(item.id,'skip')));}
  if(item.status==='working')actions.append(element('span','正在生成和复核，初稿会出现在“已生成”或“待复核”。','hint'));
  if(item.status==='done'||item.status==='review'){
    actions.append(button('查看初稿',()=>preview(item.id)));
    if(session.vault_enabled&&!item.imported)actions.append(button('导入 Markdown 库',()=>importNote(item.id),true));
    const link=element('a','下载 Markdown','button');link.href=`/api/items/${item.id}/download`;actions.append(link);
    if(item.imported)card.append(element('p','已入库，后续请在内容库中继续修改。','import-result'));
    else if(!session.vault_enabled)card.append(element('p','初稿尚未入库，可先下载保存。','hint'));
  }
  if(item.status==='skipped')actions.append(button('放回待整理',()=>action(item.id,'restore')));
  if(item.status==='attention'){
    if(item.recovery_available){const repair=button('修复并重新复核',()=>recoverNote(item.id),true);repair.disabled=!session.model_enabled;actions.append(repair);}
    else if(item.recovery_unavailable_reason)card.append(element('p',item.recovery_unavailable_reason,'hint'));
    else if(item.recovery_version)card.append(element('p','本轮修复记录已保留，不会自动再次调用模型。','hint'));
    actions.append(button('略过',()=>action(item.id,'skip')));
  }
  card.append(actions,details||excerptsFor(item.id));return card;
}
function emptyState(){const box=element('div',undefined,'empty');box.append(element('h3',query?'没有找到匹配的材料':'这个分类暂时没有材料'),element('p',query?'试试更短的标题或摘要关键词。':view==='done'?'复核通过的初稿会出现在这里。':view==='review'?'已保存但仍需复核的初稿会出现在这里。':'可以切换到其他分类继续阅读。'));if(query)box.append(button('清空搜索',clearSearch));else if(view!=='todo')box.append(button('查看待整理',()=>navigate('todo',1)));return box;}
function renderItems(nextItems){
  const container=$('items'),keep=new Set(nextItems.map(i=>i.id));
  for(const [id,record]of cards)if(!keep.has(id)){record.node.remove();cards.delete(id);}
  if(!nextItems.length){container.replaceChildren(emptyState());return;}
  for(const child of [...container.children])if(!child.classList.contains('card'))child.remove();
  let cursor=container.firstElementChild;
  for(const item of nextItems){const signature=JSON.stringify(item);let record=cards.get(item.id);
    if(!record||record.signature!==signature){const old=record?.node;const node=cardFor(item,old?.querySelector('details'));if(old){old.replaceWith(node);if(cursor===old)cursor=node;}record={node,signature};cards.set(item.id,record);}
    if(record.node!==cursor)container.insertBefore(record.node,cursor);cursor=record.node.nextElementSibling;
  }
}
async function refresh(background=false){
  if(background&&($('preview').open||document.querySelector('.card details[open]')||document.activeElement.closest('#items, #tabs, .pagination')))return;
  if(refreshing){if(!background)queued=true;return;}refreshing=true;const requested={view,query,page};if(!background)$('items').setAttribute('aria-busy','true');
  try{
    const result=await api(`/api/items?view=${requested.view}&q=${encodeURIComponent(requested.query)}&page=${requested.page}`);
    if(view!==requested.view||query!==requested.query||page!==requested.page){queued=true;return;}
    page=result.page;total=result.total;saveLocation(true);items=result.items;if($('notice').classList.contains('error-notice'))dismissNotice();
    const done=result.counts.done??0,review=result.counts.review??0;$('stats').textContent=`${Object.values(result.counts).reduce((a,b)=>a+b,0)} 条材料 · ${done+review} 篇初稿已生成${review?`（${review} 篇待复核）`:''} · ${result.imported_count??0} 篇已导入当前库`;
    for(const[key,label]of Object.entries(labels)){const b=$('tab-'+key);b.textContent=`${label} ${result.counts[key]??0}`;b.hidden=key==='review'&&!Object.hasOwn(result.counts,'review');b.classList.toggle('active',key===view);b.setAttribute('aria-current',key===view?'page':'false');}
    $('results-title').textContent=`${labels[view]}${query?' · 搜索结果':''} · ${total} 条`;
    renderItems(items);const pages=Math.max(1,Math.ceil(total/30));
    for(const suffix of['','-top']){$('page'+suffix).textContent=`第 ${page} / ${pages} 页`;$('previous'+suffix).disabled=page<=1;$('next'+suffix).disabled=page>=pages;}
    const u=result.usage;$('usage').textContent=`已记录 ${u.calls} 次模型调用，费用 $${Number(u.actual_usd).toFixed(4)}，累计预算 $${u.budget_usd}${u.unknown_cost_calls?'；部分费用待核对':''}。`;
  }catch(error){if(!items.length){const box=element('div',undefined,'empty');box.append(element('h3','材料暂时无法读取'),element('p',error.message),button('重新加载',()=>refresh()));$('items').replaceChildren(box);}else notice(`更新未完成：${error.message}`,'重新加载',()=>refresh(),true);}
  finally{refreshing=false;$('items').removeAttribute('aria-busy');if(queued){queued=false;refresh();}}
}
// Build only text and explicitly allowed DOM nodes; source HTML is never inserted.
function inline(node,text){
  const pattern=/(`+)([^`]+)\1|\*\*([^*]+)\*\*|\[([^\]]+)\]\(([^\s)]+)\)/g;let offset=0;
  for(const match of text.matchAll(pattern)){node.append(document.createTextNode(text.slice(offset,match.index)));let child;
    if(match[2])child=element('code',match[2]);else if(match[3])child=element('strong',match[3]);else{try{const url=new URL(match[5]);if(!['https:','http:'].includes(url.protocol)||url.username||url.password)throw Error();child=element('a',match[4]);child.href=url.href;child.target='_blank';child.rel='noopener noreferrer';}catch{child=document.createTextNode(match[0]);}}
    node.append(child);offset=match.index+match[0].length;
  }node.append(document.createTextNode(text.slice(offset)));return node;
}
function renderMarkdown(markdown){
  const nodes=[];let paragraph=[],code=null,fence=null,list=null;
  function flush(){if(paragraph.length){nodes.push(inline(element('p'),paragraph.join('\n')));paragraph=[];}list=null;}
  for(const line of markdown.split('\n')){
    const marker=/^\s{0,3}(`{3,}|~{3,})(.*)$/.exec(line);
    if(code!==null){if(marker&&marker[1][0]===fence[0]&&marker[1].length>=fence.length&&!marker[2].trim()){const pre=element('pre');pre.append(element('code',code.join('\n')));nodes.push(pre);code=null;fence=null;}else code.push(line);continue;}
    if(marker){flush();code=[];fence=marker[1];continue;}
    if(!line.trim()){flush();continue;}
    const heading=/^(#{1,6})\s+(.+)$/.exec(line);if(heading){flush();nodes.push(inline(element(`h${heading[1].length}`),heading[2]));continue;}
    const bullet=/^\s*(?:([-*+])|\d+[.)])\s+(.+)$/.exec(line);if(bullet){if(paragraph.length)flush();const type=bullet[1]?'ul':'ol';if(!list||list.tagName.toLowerCase()!==type){list=element(type);nodes.push(list);}list.append(inline(element('li'),bullet[2]));continue;}
    const quote=/^>\s?(.*)$/.exec(line);if(quote){flush();nodes.push(inline(element('blockquote'),quote[1]));continue;}
    if(list)flush();paragraph.push(line);
  }flush();if(code!==null){const pre=element('pre');pre.append(element('code',code.join('\n')));nodes.push(pre);}return nodes;
}
function updatePreviewNavigation(){const notes=items.filter(i=>i.status==='done'||i.status==='review'),index=notes.findIndex(i=>i.id===previewId);$('preview-previous').disabled=index<=0;$('preview-next').disabled=index<0||index>=notes.length-1;$('preview-position').textContent=index<0?'':`本页 ${index+1} / ${notes.length} 篇`;}
async function preview(id){
  const request=++previewRequest;previewController?.abort();previewController=new AbortController();previewId=id;const item=items.find(i=>i.id===id);
  if(!$('preview').open){opener=document.activeElement;$('preview').showModal();document.body.classList.add('dialog-open');}
  $('preview-title').textContent=item?.title||'笔记初稿';$('note-scroll').scrollTop=0;$('note-body').setAttribute('aria-busy','true');$('note-body').replaceChildren(element('p','正在打开初稿…','loading-label'));$('preview-status').textContent='正在读取初稿…';showReceipt(null);$('download').hidden=true;$('import-preview').hidden=true;updatePreviewNavigation();
  try{const data=await api(`/api/items/${id}/note`,false,previewController.signal);if(request!==previewRequest||!$('preview').open)return;
    const receipt=Object.hasOwn(data,'imported')?data.imported:item?.imported;$('preview-title').textContent=data.title;$('note-body').replaceChildren(...renderMarkdown(data.markdown));$('download').href=`/api/items/${id}/download`;$('download').hidden=false;previewStatus(receipt,data.review_status);$('import-preview').hidden=!session.vault_enabled||!!receipt;if(!$('preview').contains(document.activeElement))$('preview-title').focus({preventScroll:true});
  }catch(error){if(error.name==='AbortError'||request!==previewRequest)return;$('preview-status').textContent='初稿尚未打开，材料仍然保留。';$('note-body').replaceChildren(element('p',error.message,'error'),button('重新打开初稿',()=>preview(id)));}
  finally{if(request===previewRequest)$('note-body').removeAttribute('aria-busy');}
}
function stepPreview(step){const notes=items.filter(i=>i.status==='done'||i.status==='review'),index=notes.findIndex(i=>i.id===previewId),next=notes[index+step];if(next)preview(next.id);}
$('import-preview').addEventListener('click',()=>run($('import-preview'),()=>importNote(previewId)));
$('close-preview').addEventListener('click',()=>$('preview').close());
$('preview').addEventListener('close',()=>{previewId=null;++previewRequest;previewController?.abort();document.body.classList.remove('dialog-open');(opener?.isConnected?opener:$('results-title')).focus({preventScroll:true});});
$('preview-previous').addEventListener('click',()=>stepPreview(-1));$('preview-next').addEventListener('click',()=>stepPreview(1));
$('preview').addEventListener('keydown',event=>{if(event.altKey&&['ArrowLeft','ArrowRight'].includes(event.key)){event.preventDefault();stepPreview(event.key==='ArrowLeft'?-1:1);}});
for(const suffix of['','-top']){$('previous'+suffix).addEventListener('click',()=>navigate(view,page-1));$('next'+suffix).addEventListener('click',()=>navigate(view,page+1));}
function setSearch(value){query=value.slice(0,300);page=1;$('search').value=query;$('clear-search').hidden=!query;saveLocation(true);refresh();}
function clearSearch(){clearTimeout(searchTimer);setSearch('');$('search').focus();}
$('search').addEventListener('input',()=>{clearTimeout(searchTimer);$('clear-search').hidden=!$('search').value;searchTimer=setTimeout(()=>setSearch($('search').value),250);});
$('search').addEventListener('keydown',event=>{if(event.key==='Escape'&&$('search').value){event.preventDefault();clearSearch();}});
$('clear-search').addEventListener('click',clearSearch);$('dismiss-notice').addEventListener('click',dismissNotice);
addEventListener('popstate',()=>{clearTimeout(searchTimer);readLocation();refresh();});
async function start(){readLocation();try{session=await api('/api/session');
  $('tabs').replaceChildren();for(const[key,label]of Object.entries(labels)){const b=button(label,()=>navigate(key,1));b.id='tab-'+key;$('tabs').append(b);}
  $('library-status').textContent=session.vault_enabled?'已连接内容库':'尚未配置内容库';
  $('library-location').textContent=session.vault_enabled?`主动导入的初稿保存在服务器内容库：${session.vault_path}。请通过你已配置的 Obsidian 同步连接查看，后续修改在内容库中继续。`:'未配置内容库时，可以浏览和下载初稿，稍后手动入库。';
  $('model-status').textContent=session.model_enabled?'点击“整理成笔记”或确认“修复并重新复核”才会使用模型，浏览、导入和下载不调用模型。':'模型调用已关闭；仍可阅读、略过、下载和导入已有初稿。';
  await refresh();
}catch(error){const box=element('div',undefined,'empty');box.append(element('h3','暂时无法连接收件箱'),element('p',error.message),button('重新连接',start));$('items').replaceChildren(box);$('items').removeAttribute('aria-busy');}}
start();setInterval(()=>{if(session)refresh(true);},6000);
