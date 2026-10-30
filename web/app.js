const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="purple-session"]').content;
let selected = null, current = null, lastEventCount = -1, settingsLoaded = false, toastTimer;
let polling = false;
function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 6500); }
async function api(path, data, method) {
  const response = await fetch(path, {method: method || (data ? 'POST' : 'GET'), headers: {'X-Purple-Session': token, ...(data ? {'Content-Type':'application/json'} : {})}, body: data ? JSON.stringify(data) : undefined, signal: AbortSignal.timeout(120000)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Request failed');
  return result;
}
function showView(name) { for (const view of ['workspace','providers']) $(view+'-view').hidden = view !== name; document.querySelectorAll('[data-view]').forEach(b => b.classList.toggle('active', b.dataset.view === name)); $('page-name').textContent = name[0].toUpperCase()+name.slice(1); }
document.querySelectorAll('[data-view]').forEach(b => b.addEventListener('click', () => showView(b.dataset.view)));
function keyState(settings) {
  for (const name of ['nvidia','openrouter']) $(''+name+'-key-state').textContent = settings[name+'_key_set'] ? '· configured' : '· not configured';
  $('key-dot').classList.toggle('connected', settings.nvidia_key_set || settings.openrouter_key_set);
}
async function refresh() {
  if (polling) return;
  polling = true;
  try {
    const [status, jobs] = await Promise.all([api('/api/status'), api('/api/jobs')]);
    keyState(status.settings);
    if (!settingsLoaded) { for (const name of ['nvidia','openrouter']) $(name+'-model').value = status.settings[name+'_model']; $('daily-cap').value = status.settings.daily_cap; $('remember-keys').checked = status.settings.remember_keys; settingsLoaded = true; }
    $('metric-quota').textContent = `${status.quota.attempts} / ${status.settings.daily_cap} local cap`;
    $('sandbox-message').textContent = status.preparing ? 'Preparing container image…' : status.sandbox.message;
    $('sandbox-dot').classList.toggle('ready', status.sandbox.ready);
    $('prepare-sandbox').disabled = status.preparing || status.sandbox.ready;
    $('prepare-sandbox').textContent = status.sandbox.ready ? 'Ready' : 'Prepare sandbox';
    $('run-task').disabled = !!status.active; $('quick-demo').disabled = !!status.active;
    $('task-count').textContent = jobs.length;
    if (!selected && jobs.length) selected = jobs[0].id;
    if (jobs.length) {
      $('history').replaceChildren();
      for (const job of jobs) { const button = document.createElement('button'); button.textContent = job.title; const small = document.createElement('small'); small.textContent = `${job.status} · ${job.mode}`; button.append(small); button.classList.toggle('selected', job.id === selected); button.addEventListener('click', () => { selected = job.id; lastEventCount = -1; $('file-preview').hidden = true; showView('workspace'); refresh(); }); $('history').append(button); }
    }
    if (selected) renderJob(await api('/api/job?id='+selected));
  } catch (error) { if (!current) $('sandbox-message').textContent = 'Connection interrupted. Keep the Python app running.'; }
  finally { polling = false; }
}
function renderJob(job) {
  current = job;
  $('metric-status').textContent = job.status[0].toUpperCase()+job.status.slice(1);
  $('metric-provider').textContent = job.provider || 'Waiting for provider';
  $('metric-steps').textContent = `${job.steps} / ${job.max_steps}`;
  $('live-indicator').textContent = job.mode === 'demo' ? 'SCRIPTED DEMO' : job.status.toUpperCase();
  $('stop-task').hidden = job.status !== 'running';
  $('resume-task').hidden = job.status !== 'paused' || job.steps >= job.max_steps;
  $('task-reason').hidden = !job.reason; $('task-reason').textContent = job.reason;
  if (job.events.length !== lastEventCount) {
    const feed = $('activity'), nearBottom = feed.scrollHeight-feed.scrollTop-feed.clientHeight < 100;
    feed.replaceChildren();
    for (const event of job.events) {
      const item = document.createElement('div'); item.className = 'event '+event.kind;
      const header = document.createElement('div'); header.className = 'event-header';
      const kind = document.createElement('span'); kind.textContent = event.kind;
      const time = document.createElement('span'); time.textContent = new Date(event.at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'});
      header.append(kind,time); item.append(header);
      if (event.kind === 'result') { const details = document.createElement('details'), summary = document.createElement('summary'), code = document.createElement('pre'); summary.textContent = 'Inspect tool result'; try { code.textContent = JSON.stringify(JSON.parse(event.message),null,2); } catch { code.textContent = event.message; } details.append(summary,code); item.append(details); }
      else { const text = document.createElement('p'); text.textContent = event.message; item.append(text); }
      feed.append(item);
    }
    if (nearBottom || lastEventCount === -1) feed.scrollTop = feed.scrollHeight;
    lastEventCount = job.events.length;
  }
  $('file-count').textContent = job.files.length;
  $('export-files').disabled = !job.files.length;
  $('file-list').replaceChildren();
  if (!job.files.length) { const p = document.createElement('p'); p.className = 'muted'; p.textContent = 'No files yet.'; $('file-list').append(p); }
  for (const file of job.files) { const b = document.createElement('button'); b.textContent = file.path; const size = document.createElement('small'); size.textContent = `${file.bytes} B`; b.append(size); b.addEventListener('click', async () => { try { const result = await api('/api/file?id='+job.id+'&path='+encodeURIComponent(file.path)); $('file-preview').textContent = result.content; $('file-preview').hidden = false; } catch (error) { toast(error.message); } }); $('file-list').append(b); }
  $('report').textContent = job.report || 'The final report will show what changed, what was tested, and what still needs attention.';
}
async function launch(body) { try { const job = await api('/api/task', body); selected = job.id; lastEventCount = -1; $('file-preview').hidden = true; showView('workspace'); await refresh(); } catch (error) { toast(error.message); } }
$('task-form').addEventListener('submit', event => { event.preventDefault(); launch({task:$('task').value,mode:$('mode').value,lab:$('lab').checked,max_steps:Number($('max-steps').value)}); });
$('quick-demo').addEventListener('click', () => launch({task:'Demonstrate the local workflow by creating a Python greeting function, adding a test, and checking syntax.',mode:'demo',max_steps:10}));
$('coding-preset').addEventListener('click', () => { $('mode').value='live'; $('lab').checked=false; $('task').value='Build a Python command-line log summarizer using only the standard library. Read JSON Lines events, count successful and failed logins, and print a clear summary. Add regression tests including empty and malformed input. Run the tests and report the results.'; });
$('lab-preset').addEventListener('click', () => { $('mode').value='live'; $('lab').checked=true; $('task').value='Audit the provided local lab.py login fixture. Write a test demonstrating its SQL injection issue, fix it using parameterized queries, and add regression tests for valid logins, invalid logins, and injection input. Verify that events record failed login attempts without logging passwords. Run the tests and write an evidence-based report.'; });
$('stop-task').addEventListener('click', async () => { if (!selected) return; try { const r=await api('/api/stop',{id:selected}); toast(r.message); } catch(e){toast(e.message);} });
$('resume-task').addEventListener('click', async () => { try { await api('/api/resume',{id:selected}); await refresh(); } catch(e){toast(e.message);} });
$('prepare-sandbox').addEventListener('click', async () => { try { const r=await api('/api/sandbox',{}); toast(r.message); refresh(); } catch(e){toast(e.message);} });
$('settings-form').addEventListener('submit', async event => { event.preventDefault(); const body = {daily_cap:Number($('daily-cap').value), remember_keys:$('remember-keys').checked}; for(const name of ['nvidia','openrouter']) { body[name+'_key']=$(name+'-key').value; body[name+'_model']=$(name+'-model').value; body['clear_'+name+'_key']=$('clear-'+name).checked; } try { const settings=await api('/api/settings',body); keyState(settings); for(const name of ['nvidia','openrouter']) { $(name+'-key').value=''; $('clear-'+name).checked=false; } toast('Provider settings saved.'); refresh(); } catch(e){toast(e.message);} });
for (const name of ['nvidia','openrouter']) $('test-'+name).addEventListener('click', async () => { const b=$('test-'+name); b.disabled=true; b.textContent='Testing…'; try { const r=await api('/api/provider-test',{provider:name}); toast(r.message); } catch(e){toast(e.message);} finally {b.disabled=false;b.textContent='Test connection';refresh();} });
$('export-files').addEventListener('click',async()=>{try{const response=await fetch('/api/export?id='+selected,{headers:{'X-Purple-Session':token}});if(!response.ok)throw new Error('Export failed');const url=URL.createObjectURL(await response.blob());const link=document.createElement('a');link.href=url;link.download='purple-desk-workspace.zip';link.click();setTimeout(()=>URL.revokeObjectURL(url),10000);}catch(e){toast(e.message);}});
refresh();setInterval(refresh,2000);
