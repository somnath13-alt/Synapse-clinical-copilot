const $ = (id) => document.getElementById(id);
let currentInteraction = null;
let currentFeedback = null;

async function request(path, options = {}) {
  const response = await fetch(path, {headers: {'Content-Type': 'application/json'}, ...options});
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail || 'Request failed');
  return body;
}

function render(result) {
  currentInteraction = result.interaction_id;
  $('results').classList.remove('hidden');
  $('policy-chip').textContent = result.policy_version_id ? result.policy_version_id.replace('DV-SYN-POL-VEL-', 'Policy ') + ' CURRENT' : 'Policy unverified';
  $('trace').innerHTML = result.orchestration_trace.map(item => `<div class="trace-item ${item.status !== 'RETRIEVED' ? 'failed' : ''}"><b>${item.sequence}. ${item.label}</b><span>${item.status.replaceAll('_',' ')}</span></div>`).join('');
  $('answer').textContent = result.answer;
  $('confidence').textContent = result.confidence ? `${result.confidence} CONFIDENCE` : 'NOT EVALUATED';
  $('confidence').className = `confidence ${result.confidence === 'LOW' ? 'low' : ''}`;
  $('rationale').textContent = result.confidence_rationale || '';
  $('reconciliation').innerHTML = result.reconciliation.map(item => `<div class="reconciliation-box"><b>${item.type.replaceAll('_',' ')}</b>${item.explanation}</div>`).join('');
  $('escalation').innerHTML = result.escalation ? `<div class="escalation"><b>Human review required</b><br>${result.escalation.reason}</div>` : '';
  $('citation-count').textContent = `${result.citations.length} claim-linked citations`;
  $('citations').innerHTML = result.citations.map(c => `<article class="citation"><header><span class="source-type">${c.source_type.replaceAll('_',' ')}</span><span class="version">v${c.version} · ${c.timestamp.slice(0,10)}</span></header><h4>${c.source_title}</h4><blockquote>“${c.relevant_excerpt}”</blockquote></article>`).join('');
  loadAudit();
  $('results').scrollIntoView({behavior:'smooth'});
}

async function ask(mode = 'BASELINE') {
  $('ask').disabled = true;
  try { render(await request('/api/v1/questions', {method:'POST', body:JSON.stringify({question:$('question').value, source_mode:mode})})); }
  catch (error) { alert(error.message); }
  finally { $('ask').disabled = false; }
}

async function loadAudit() {
  if (!currentInteraction) return;
  const data = await request(`/api/v1/audit/${currentInteraction}`);
  $('audit').innerHTML = data.events.length ? data.events.map(event => `<div class="event"><b>${event.event_type.replaceAll('_',' ')}</b><time>${event.occurred_at}</time></div>`).join('') : '<p class="muted">No events yet.</p>';
}

$('ask').onclick = () => ask();
$('missing').onclick = () => ask('PAYER_POLICY_UNAVAILABLE');
$('conflict').onclick = () => ask('TEST_ONLY_FORMULARY_SUBSTITUTION');
$('refresh-audit').onclick = loadAudit;
$('submit-feedback').onclick = async () => {
  if (!currentInteraction) return;
  try {
    const result = await request('/api/v1/feedback', {method:'POST', body:JSON.stringify({interaction_id:currentInteraction, message:'Payer policy V1 is outdated; use V2.'})});
    currentFeedback = result.feedback_id;
    $('feedback-status').innerHTML = `<div class="status-message"><b>PENDING</b> · V1 remains current until review.</div>`;
    $('approve').classList.remove('hidden');
    loadAudit();
  } catch (error) { alert(error.message); }
};
$('approve').onclick = async () => {
  try {
    const result = await request(`/api/v1/feedback/${currentFeedback}/approve`, {method:'POST', body:JSON.stringify({})});
    $('feedback-status').innerHTML = `<div class="status-message"><b>APPROVED + APPLIED</b> · V2 is now current. Ask the same question again.</div>`;
    $('approve').classList.add('hidden');
    loadAudit();
  } catch (error) { alert(error.message); }
};
$('reset').onclick = async () => {
  if (!confirm('Restore the complete synthetic V1 baseline?')) return;
  try {
    await request('/api/demo/reset', {method:'POST', body:JSON.stringify({confirmation:'RESET_SYNTHETIC_DEMO'})});
    currentInteraction = currentFeedback = null;
    $('results').classList.add('hidden');
    $('feedback-status').innerHTML = '';
    $('approve').classList.add('hidden');
  } catch (error) { alert(error.message); }
};
