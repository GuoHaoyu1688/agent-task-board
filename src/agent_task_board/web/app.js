'use strict';
const $ = id => document.getElementById(id);
const statuses = ['todo','claimed','running','review','done','failed','blocked','cancelled'];
let state = {tasks: [], read_only: true}, selected = null;
function element(tag, text, className) { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if(className)node.className=className; return node; }
function date(value) { return value ? new Date(value).toLocaleString('en-GB', {timeZone:state.timezone || 'Asia/Shanghai'}) : 'No deadline'; }
async function api(path, payload) {
  const response = await fetch(path, payload ? {method:'POST',headers:{'Content-Type':'application/json','X-ATB-CSRF':state.csrf},body:JSON.stringify(payload)} : {});
  const result = await response.json(); if(!result.ok) throw new Error(result.error.message); return result.data;
}
async function refresh() {
  try { state = await api('/api/state?agent='+encodeURIComponent($('agent').value)); $('mode').textContent=state.read_only?'Read-only':'Local workspace'; $('add-toggle').hidden=state.read_only; $('summary').textContent=state.tasks.length+' tasks · '+date(state.now); render(); if(selected)await details(selected); }
  catch(error){$('notice').textContent=error.message;}
}
function render() {
  $('board').replaceChildren();
  for (const status of statuses) {
    const tasks=state.tasks.filter(t=>t.status===status), column=element('section',undefined,'column'), title=element('h2',status);
    title.append(element('span',tasks.length,'count')); column.append(title);
    for(const task of tasks){
      const overdue=task.due_at && new Date(task.due_at)<new Date(state.now) && !['done','cancelled'].includes(task.status);
      const card=element('button',undefined,'card'+(overdue?' overdue':'')); card.append(element('h3',task.title),element('p',task.queue+' / priority '+task.priority,'queue'),element('p',task.lease_owner||task.assignee||'Any available agent'),element('p',date(task.due_at),'due'));
      task.tags.forEach(tag=>card.append(element('span',tag,'tag'))); card.onclick=()=>details(task.id); column.append(card);
    }
    $('board').append(column);
  }
}
async function action(path,payload) { try {await api(path,payload);$('notice').textContent='Saved';await refresh();}catch(error){$('notice').textContent=error.message;} }
async function details(id) {
  selected=id;const task=state.tasks.find(t=>t.id===id);if(!task){$('detail').hidden=true;selected=null;return;}
  $('detail').hidden=false;$('detail-title').textContent=task.title;$('detail-meta').textContent=task.id+' · '+task.status+' · attempts '+task.attempts+'/'+task.max_attempts;
  $('detail-body').textContent=task.body+'\n\nInputs: '+JSON.stringify(task.params,null,2)+'\nResult: '+JSON.stringify(task.result,null,2)+'\nArtifacts: '+task.artifacts.join(', ')+'\nDependencies: '+task.blocked_by.join(', ');
  $('detail-actions').replaceChildren();
  if(!state.read_only){
    if(task.status==='review')for(const approved of [true,false]){const button=element('button',approved?'Approve':'Return for revision',approved?'primary':'');button.onclick=()=>action('/api/review',{id,approved,reason:prompt('Review note (optional)')||''});$('detail-actions').append(button);}
    const comment=element('button','Add hand-off note');comment.onclick=()=>{const text=prompt('Hand-off note');if(text)action('/api/comment',{id,text});};$('detail-actions').append(comment);
    if(!['done','failed','cancelled'].includes(task.status)){const assign=element('button','Reassign');assign.onclick=()=>{const assignee=prompt('Agent name (empty = any agent)',task.assignee||'');if(assignee!==null)action('/api/reassign',{id,assignee:assignee||null});};$('detail-actions').append(assign);}
  }
  const events=await api('/api/events?task='+encodeURIComponent(id));$('timeline').replaceChildren();for(const event of events.reverse()){const li=element('li',event.actor+' · '+event.type+' '+JSON.stringify(event.payload));li.append(element('time',date(event.at)));$('timeline').append(li);}
}
$('refresh').onclick=refresh;$('agent').onchange=refresh;$('close-detail').onclick=()=>{selected=null;$('detail').hidden=true;};$('add-toggle').onclick=()=>{$('new-task').hidden=!$('new-task').hidden;};
$('new-task').onsubmit=async event=>{event.preventDefault();const form=new FormData(event.target),task=Object.fromEntries(form);task.priority=Number(task.priority);task.needs_approval=form.has('needs_approval');task.assignee=task.assignee||null;task.due_at=task.due_at||null;try{await api('/api/tasks',task);event.target.reset();$('new-task').hidden=true;await refresh();}catch(error){$('notice').textContent=error.message;}};
refresh();setInterval(refresh,10000);
