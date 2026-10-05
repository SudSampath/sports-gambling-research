import {count, money, probability, edge, words, short, timestamp, inventory, filterCandidates, title} from './format.js';

const repository = 'https://github.com/SudSampath/sports-gambling-research';
let source = `${repository}/blob/main`;
const $ = selector => document.querySelector(selector);
const node = (tag, className = '', text = '') => {
  const element = document.createElement(tag);
  element.className = className;
  element.textContent = text;
  return element;
};
const append = (parent, ...children) => {parent.append(...children); return parent;};
const link = (text, href, className = '') => {
  const element = node('a', className, text);
  element.href = href; element.target = '_blank'; element.rel = 'noreferrer'; return element;
};
const badge = (text, kind = '') => node('span', `badge ${kind}`, text);
const section = (heading, subtitle) => {
  const element = node('section', 'panel');
  append(element, append(node('div', 'panel-heading'), node('h2', '', heading), node('p', '', subtitle))); return element;
};
const fact = (label, value, detail = '') => append(node('div', 'metric'), node('span', 'field-label', label), node('strong', '', value), node('p', '', detail));
const detailRow = (label, value) => append(node('div', 'detail-row'), node('dt', '', label), node('dd', '', value));
const state = {campaigns: [], proof: null, selected: 0, view: 'overview', search: '', filter: 'all', page: 0};
const viewNames = {overview: 'Overview', candidates: 'Candidates', portfolio: 'Paper portfolio', restate: 'How Restate works'};
const headings = {
  overview: ['A clearer view of the market.', 'Explore what the system found, what it rejected, and why.'],
  candidates: ['Every candidate has a reason.', 'Inspect supported forecasts, unknown outcomes, and the inputs behind each decision.'],
  portfolio: ['Fictional capital. Real constraints.', 'Follow reservations, conservative fills, reconciliation, and settlement.'],
  restate: ['Research that survives a restart.', 'Where durable orchestration earns its place—and where it adds overhead.'],
};

function navigate(view) {state.view = view; state.page = 0; render();}
for (const button of $('#navigation').querySelectorAll('button')) button.addEventListener('click', () => navigate(button.dataset.view));
$('#campaign-picker').addEventListener('change', event => {state.selected = Number(event.target.value); state.page = 0; state.search = ''; state.filter = 'all'; render();});
$('#close-dialog').addEventListener('click', () => $('#candidate-dialog').close());
$('#candidate-dialog').addEventListener('click', event => {if (event.target === $('#candidate-dialog')) $('#candidate-dialog').close();});

function render() {
  const campaign = state.campaigns[state.selected];
  if (!campaign) return;
  const [heading, subtitle] = headings[state.view];
  $('#page-title').textContent = heading; $('#page-subtitle').textContent = subtitle;
  $('#view-label').textContent = viewNames[state.view].toUpperCase();
  for (const button of $('#navigation').querySelectorAll('button')) {
    button.classList.toggle('active', button.dataset.view === state.view);
    button.setAttribute('aria-current', button.dataset.view === state.view ? 'page' : 'false');
  }
  $('#campaign-picker').value = state.selected;
  $('#campaign-meta').replaceChildren(badge(campaign.mode === 'public' ? 'Public market data' : 'Synthetic test', campaign.mode === 'public' ? 'green' : 'amber'), badge(words(campaign.status), 'muted'), node('span', '', `Observed ${timestamp(campaign.started_at)}`));
  $('#download-report').href = `./data/${campaign.filename}`;
  $('#view').replaceChildren({overview, candidates, portfolio, restate}[state.view](campaign));
  $('#research-status').textContent = `${viewNames[state.view]}. ${title(campaign)}. ${count(campaign.completed_candidates)} completed candidates, ${campaign.eligible_decisions} eligible decisions.`;
  const url = new URL(location.href); url.searchParams.set('view', state.view); url.searchParams.set('campaign', campaign.public_id);
  history.replaceState(null, '', url);
}

function overview(campaign) {
  const container = node('div', 'view-content');
  const hero = node('div', 'finding');
  const body = node('div');
  append(body, node('p', 'eyebrow', 'CAMPAIGN FINDING'), node('h2', '', campaign.mode === 'public' ? `${count(campaign.genuine_live_candidates)} markets screened. ${count(campaign.eligible_decisions)} eligible decisions.` : `${count(campaign.completed_candidates)} candidates. ${count(campaign.filled_position_total)} paper positions with fills.`),
    node('p', '', campaign.mode === 'public' ? (campaign.eligible_decisions === 0 ? 'The run found no supported, exactly matched opportunity. Unsupported probabilities remain unknown. Zero is a valid research result.' : 'These archived decisions passed forecast screening. Conservative execution and the fixed portfolio risk gate determine whether any paper inventory is opened.') : 'Engineered fixtures exercise orchestration and risk controls. Their probabilities, prices, and results do not establish a market edge.'));
  const emblem = append(node('div', 'finding-emblem'), node('span', '', '↗'), node('small', '', 'EVIDENCE FIRST'));
  append(hero, body, emblem); append(container, hero);
  append(container, append(node('div', 'metrics'),
    fact(campaign.mode === 'public' ? 'GENUINE MARKETS DISCOVERED' : 'SYNTHETIC CANDIDATES', count(campaign.mode === 'public' ? campaign.genuine_live_candidates : campaign.synthetic_candidates), campaign.mode === 'public' ? 'Public Gamma discovery · unique identities' : 'Test fixtures · 0 genuine live markets'),
    fact('ELIGIBLE DECISIONS', count(campaign.eligible_decisions), 'After contract and forecast screening'),
    fact('PAPER POSITIONS WITH FILLS', count(campaign.filled_position_total), `${campaign.position_total} total lifecycle / admission records`),
    fact('RESERVED CAPITAL', money(campaign.capital.reserved), 'Fictional USD · shared portfolio')));

  const grid = node('div', 'two-column');
  const coverage = section('Where the candidates went', 'Screen first. Forecast only supported, exactly matched contracts.');
  const outcomes = [['Eligible for paper evaluation', campaign.eligible_decisions, 'eligible'], ...Object.entries(campaign.exclusions).map(([reason, amount]) => [words(reason), amount, 'excluded'])];
  for (const [reason, amount, kind] of outcomes) {
    const line = append(node('div', 'coverage-row'), append(node('div', 'coverage-label'), node('span', '', reason), node('strong', '', count(amount))));
    const bar = node('div', `coverage-fill ${kind}`); bar.style.width = `${Math.max(0, amount / (campaign.completed_candidates || 1) * 100)}%`;
    append(line, append(node('div', 'coverage-track'), bar)); append(coverage, line);
  }
  append(coverage, node('p', 'footnote', `${count(campaign.completed_candidates)} completed analyses · ${campaign.pages} discovery pages · ${campaign.requests} public GET requests. Exclusion reasons may overlap.`));
  const integrity = section('Know what the evidence supports', 'Coverage and orchestration are separate from predictive performance.');
  const limitations = campaign.mode === 'public' ? [
    ['Exact contract coverage', campaign.missing_audited_catalog ? 'This run had no audited contract catalog. No live forecast-to-fill opportunity was established.' : 'This run used an audited contract catalog. Each decision binds the outcome and settlement-rule version.'],
    ['Model support', 'The current winner model supports postseason NFL outcomes. Regular-season ties and other contract families remain unsupported.'],
    ['Execution evidence', campaign.eligible_decisions === 0 ? 'No public market trade was eligible; there is no live paper-return result to report.' : 'Eligibility alone does not imply a fill or a profitable result. Inspect the paper portfolio for admission, execution and settlement outcomes.'],
  ] : [
    ['Synthetic outcomes', 'Fixtures use deliberately favorable prices and an uncalibrated model version. Apparent edge is test input.'],
    ['Conservative fills', `Ask-side depth, fees, ${campaign.policy.latency_ms} ms latency, and ${probability(campaign.policy.depth_fraction)} displayed depth. ${campaign.filled_position_total} positions with fills; ${campaign.policy.decision_ttl_seconds}-second decision TTL. No midpoint assumptions.`],
    ['Outcome evidence', campaign.settlement_total ? 'Settlement uses a virtual future timestamp and synthetic final rules. Any realized result is fictional test evidence.' : 'No final settlement is recorded for this campaign. Pending paper inventory provides no realized return evidence.'],
  ];
  for (const [name, explanation] of limitations) append(integrity, append(node('div', 'insight'), node('span', 'insight-mark', '↳'), append(node('div'), node('h3', '', name), node('p', '', explanation))));
  append(grid, coverage, integrity); append(container, grid);

  const campaigns = section('Published campaigns', 'Switch datasets to explore their decisions and portfolio records. These are archived observations, not a running feed.');
  const cards = node('div', 'campaign-cards');
  state.campaigns.forEach((item, index) => {
    const card = node('button', `campaign-card ${state.selected === index ? 'selected' : ''}`);
    card.setAttribute('aria-label', `Explore ${title(item)} (${item.mode})`);
    append(card, badge(item.mode === 'public' ? 'PUBLIC SCREEN' : 'SYNTHETIC', item.mode === 'public' ? 'green' : 'amber'), node('span', 'campaign-title', title(item)), node('strong', '', count(item.completed_candidates)), node('span', 'campaign-description', `${item.eligible_decisions} eligible · ${item.filled_position_total} positions with fills`), node('span', 'quiet-link', 'Explore campaign →'));
    card.addEventListener('click', () => {state.selected = index; state.page = 0; state.search = ''; state.filter = 'all'; render();}); append(cards, card);
  });
  append(campaigns, cards); append(container, campaigns);
  append(container, append(node('div', 'evidence-strip'), node('div', '', 'RESTATED, REPLAYED, RECONCILED'), node('strong', '', `${state.proof.stress.candidates_per_second.toFixed(1)} candidates / second`), node('span', '', 'Measured synthetic workload · no public API requests'), link('Inspect recovery evidence →', '?view=restate', 'quiet-link')));
  container.querySelector('.evidence-strip a').removeAttribute('target');
  container.querySelector('.evidence-strip a').addEventListener('click', event => {event.preventDefault(); navigate('restate');});
  return container;
}

function table(headers, rows) {
  const element = node('table');
  const head = node('thead'); const heading = node('tr');
  headers.forEach(text => {const th = node('th', '', text); th.scope = 'col'; append(heading, th);}); append(head, heading);
  const body = node('tbody');
  rows.forEach(cells => {const row = node('tr'); cells.forEach(value => append(row, value instanceof HTMLElement ? append(node('td'), value) : node('td', '', value))); append(body, row);});
  append(element, head, body); return append(node('div', 'table-scroll'), element);
}

function candidates(campaign) {
  const container = node('div', 'view-content');
  const panel = section('Candidate decisions', `Showing a disclosed sample: ${campaign.candidates.length} of ${count(campaign.candidate_total)} decisions. Up to 100 eligible and 100 excluded; ranked by cost-adjusted edge within each group.`);
  const toolbar = node('div', 'table-toolbar');
  const input = node('input'); input.type = 'search'; input.placeholder = 'Search contract, decision, or reason'; input.value = state.search; input.setAttribute('aria-label', 'Search candidate decisions');
  const select = node('select'); select.setAttribute('aria-label', 'Filter candidate eligibility');
  for (const [value, label] of [['all', 'All decisions'], ['eligible', 'Eligible only'], ['excluded', 'Excluded only']]) {const option = node('option', '', label); option.value = value; append(select, option);} select.value = state.filter;
  append(toolbar, input, select); append(panel, toolbar);
  const result = node('div'); append(panel, result);
  const update = (focus = null) => {
    const items = filterCandidates(campaign.candidates, state.search, state.filter);
    state.page = Math.min(state.page, Math.max(0, Math.ceil(items.length / 10) - 1));
    const slice = items.slice(state.page * 10, state.page * 10 + 10);
    const rows = slice.map(candidate => {
      const button = node('button', 'record-button', short(candidate.contract)); button.title = candidate.contract; button.addEventListener('click', () => openCandidate(candidate, campaign));
      return [button, badge(candidate.eligible ? 'Eligible' : 'Excluded', candidate.eligible ? 'green' : 'muted'), probability(candidate.probability), edge(candidate.net_edge), node('span', 'reason', candidate.reasons.map(words).join(', ')), node('span', 'model-tag', candidate.calibration_version === 'unknown' ? 'Unknown' : candidate.calibration_version)];
    });
    const body = slice.length ? table(['Contract / record', 'Decision', 'Model probability', 'Net edge', 'Reason', 'Calibration'], rows) : node('div', 'empty-state', 'No candidate records match this filter.');
    const pagination = node('div', 'pagination');
    const previous = node('button', 'outline-button', '← Previous'); previous.disabled = !state.page;
    const next = node('button', 'outline-button', 'Next →'); next.disabled = (state.page + 1) * 10 >= items.length;
    previous.addEventListener('click', () => {state.page--; update('previous');}); next.addEventListener('click', () => {state.page++; update('next');});
    append(pagination, node('span', '', `${count(items.length)} matching sampled records · page ${state.page + 1} of ${Math.max(1, Math.ceil(items.length / 10))}`), append(node('div'), previous, next));
    result.replaceChildren(body, pagination);
    $('#research-status').textContent = `${items.length} matching sampled decisions, page ${state.page + 1} of ${Math.max(1, Math.ceil(items.length / 10))}.`;
    if (focus) {
      const preferred = focus === 'next' ? next : previous;
      const alternate = focus === 'next' ? previous : next;
      (preferred.disabled ? (alternate.disabled ? input : alternate) : preferred).focus();
    }
  };
  input.addEventListener('input', event => {state.search = event.target.value; state.page = 0; update();});
  select.addEventListener('change', event => {state.filter = event.target.value; state.page = 0; update();}); update();
  append(container, panel, append(node('div', 'note'), node('strong', '', 'Interpretation matters.'), node('p', '', 'An eligible decision is permission to attempt conservative paper execution within the risk gate. Exposure limits, stale books, or latency can still prevent a fill. Synthetic forecast probabilities do not measure live predictive accuracy.')));
  return container;
}

function openCandidate(candidate, campaign) {
  const body = $('#candidate-detail');
  const heading = node('h2', '', short(candidate.contract)); heading.id = 'candidate-title';
  const facts = node('dl', 'record-details');
  for (const [name, value] of [
    ['Source', campaign.mode === 'public' ? 'Public market snapshot' : 'Synthetic test fixture'],
    ['Decision', candidate.id], ['Contract', candidate.contract], ['Market', candidate.market_id], ['Event', candidate.event],
    ['Outcome token', candidate.asset_id || 'Unknown: excluded before exact outcome analysis'], ['Forecast', candidate.forecast_id || 'None'],
    ['Status', candidate.eligible ? 'Eligible for paper risk evaluation' : 'Excluded'],
    ['Reasons', candidate.reasons.map(words).join(', ')], ['Model probability', probability(candidate.probability)],
    ['Cost-adjusted net edge', edge(candidate.net_edge)], ['Limit price', candidate.limit_price === null ? 'Unknown' : money(candidate.limit_price)],
    ['Uncertainty cost', edge(candidate.uncertainty_cost)],
    ['Information cutoff (UTC)', candidate.feature_cutoff_at], ['Decision timestamp (UTC)', candidate.decision_at],
    ['Decision expiry (UTC)', candidate.expires_at], ['Model version', candidate.model_version],
    ['Calibration', candidate.calibration_version], ['Strategy', candidate.strategy_version],
    ['Settlement-rule SHA-256', candidate.rule_version], ['Input snapshot SHA-256', candidate.input_digest], ['Policy SHA-256', candidate.policy_fingerprint],
    ['Book snapshots', candidate.snapshot_ids.join(', ') || 'None: excluded before book / forecast stage'],
  ]) append(facts, detailRow(name, value));
  body.replaceChildren(heading, node('p', 'footnote', 'Archived decision. Quotes and admission deadlines are historical; this record cannot execute a trade.'), facts);
  $('#candidate-dialog').showModal(); $('#close-dialog').focus();
}

function portfolio(campaign) {
  const container = node('div', 'view-content');
  append(container, append(node('div', 'note'), node('strong', '', 'Fictional USD · shared portfolio balances'), node('p', '', 'Positions below belong to this campaign. Cash and reservations describe the shared portfolio at export time. Realized results and marked estimates are reported separately; all synthetic results are test outcomes.')));
  const capital = campaign.capital;
  append(container, append(node('div', 'metrics'), fact('CASH', money(capital.cash), 'After simulated fills and final payouts'), fact('RESERVED', money(capital.reserved), 'Held for unfilled paper commitments'), fact('AVAILABLE', money(capital.available), 'Cash minus reservations'), fact('REALIZED PAPER RESULT', money(capital.realized_pnl), campaign.mode === 'synthetic' ? 'Synthetic outcome · no return claim' : 'Settled result only')));
  const panel = section('Position lifecycle', `${campaign.positions.length} of ${campaign.position_total} lifecycle records shown · ${inventory(campaign).length} of ${campaign.filled_position_total} positions with fills shown · ${capital.reconciled ? 'accounting reconciled' : 'reconciliation failed'} · ${capital.paused ? 'portfolio paused' : 'portfolio active at export'}`);
  if (!campaign.positions.length) append(panel, append(node('div', 'empty-state'), node('span', 'empty-symbol', '◌'), node('h3', '', 'No paper positions were opened.'), node('p', '', campaign.eligible_decisions ? 'Eligible analyses did not open paper positions in this campaign. Review its status and admission exclusions.' : 'This campaign produced no eligible opportunities. The fictional bankroll remains available.')));
  else append(panel, table(['Contract', 'Lifecycle state', 'Shares', 'Spent incl. fees', 'Still reserved', 'Admission reason'], campaign.positions.map(position => [node('span', 'monospace', short(position.contract)), badge(words(position.state), position.state === 'settled' ? 'green' : position.state === 'rejected' ? 'muted' : 'amber'), count(Number(position.shares)), money(position.spent), money(position.reservation), words(position.reason || 'accepted')])));
  append(container, panel);
  const grid = node('div', 'two-column');
  const policy = section('A fixed, versioned policy', campaign.policy.version);
  for (const [label, key] of [['Initial fictional capital', 'initial_capital'], ['Paper stake', 'stake'], ['Per contract cap', 'contract_cap'], ['Per event cap', 'event_cap'], ['Correlated group cap', 'correlated_cap'], ['Total exposure cap', 'total_cap'], ['Realized loss stop', 'loss_limit']]) append(policy, append(node('div', 'policy-row'), node('span', '', label), node('strong', '', money(campaign.policy[key]))));
  const execution = section('Execution assumptions', 'Cross executable asks conservatively; reserve before spending.');
  for (const [label, value] of [['Displayed depth available', probability(campaign.policy.depth_fraction)], ['Simulated latency', `${campaign.policy.latency_ms} ms`], ['Maximum quote age', `${campaign.policy.quote_max_age_seconds} seconds`], ['Decision TTL', `${campaign.policy.decision_ttl_seconds} seconds`]]) append(execution, append(node('div', 'policy-row'), node('span', '', label), node('strong', '', value)));
  append(execution, node('p', 'footnote', 'Fees and partial fills reduce available capital. No assumed midpoint or guaranteed maker fills. Consumed depth is not replenished by repeatedly fetching the same price level.'));
  append(execution, node('p', 'footnote', `Minimum net edge ${edge(campaign.policy.min_net_edge)}; base uncertainty haircut ${edge(campaign.policy.uncertainty_haircut)}. Uncalibrated forecasts incur additional uncertainty cost recorded in each decision.`));
  append(grid, policy, execution); append(container, grid);
  const settled = section('Settlement outcomes', `${campaign.settlements.length} of ${campaign.settlement_total} outcomes shown. ` + (campaign.mode === 'synthetic' && campaign.settlement_total ? 'Synthetic final rules use a virtual future settlement time. These are recovery-test outcomes.' : 'Final rule-verified outcomes only. Disputes retain exposure.'));
  append(settled, campaign.settlements.length ? table(['Decision', 'State', 'Payout', 'Realized result', 'Settlement timestamp'], campaign.settlements.map(item => [short(item.decision_id), words(item.state), money(item.payout), money(item.realized_pnl), timestamp(item.settled_at)])) : node('p', 'empty-state compact', 'No settlement outcomes recorded for this campaign.'));
  append(container, settled);
  const marks = section('Marked estimates', `${campaign.marked_estimates.length} of ${campaign.marked_estimate_total} estimates shown. Estimates from bid depth are unrealized, and unpriced shares are disclosed.`);
  append(marks, campaign.marked_estimates.length ? table(['Decision', 'Liquidation estimate', 'Unpriced shares', 'Marked at'], campaign.marked_estimates.map(item => [short(item.decision_id), money(item.liquidation_value), count(Number(item.unpriced_shares)), timestamp(item.marked_at)])) : node('p', 'empty-state compact', 'No marked estimates recorded. Pending inventory is not counted as realized profit.'));
  append(container, marks); return container;
}

const services = [
  {name: 'ScanCampaign', label: 'Checkpoint the campaign', role: 'Durable workflow', file: 'workflows.py',
    description: 'Persists pagination progress, bounded analysis batches and scheduled rechecks. A resumed campaign continues within its original page, request, time and candidate limits.',
    journal: 'Discovery and coherent snapshot operations are journaled with run_typed. Fan-out is bounded; durable timers govern rechecks and pause/resume checks.',
    guard: 'A terminal catalog checkpoint returns archived completion. Replaying a completed scan does not fetch a new market universe.'},
  {name: 'CandidateAnalysis', label: 'Freeze the decision inputs', role: 'Durable workflow', file: 'workflows.py',
    description: 'Rejects unsupported or ambiguous contracts before expensive forecasting. Eligible analyses bind the exact contract, point-in-time observations, book snapshots and model/strategy/rule versions.',
    journal: 'The frozen input manifest and analysis result survive worker interruption. A stable analysis ID refers to one decision, rather than the latest available information.',
    guard: 'Forecasts read only observations available at their feature cutoff. An expired decision requires a new analysis and a new quote.'},
  {name: 'PaperTrade', label: 'Preserve the trade lifecycle', role: 'Durable workflow', file: 'workflows.py',
    description: 'Coordinates reservation, simulated submission, latency, partial fills, expiry, reconciliation and rule-verified settlement. Unfilled capital is released; filled inventory remains until settlement.',
    journal: 'A durable timer models latency and expiry. When a fill commits but its acknowledgement is lost, the next attempt reads the idempotent ledger fill before doing new work.',
    guard: 'An immutable trade archive supports settlement after workflow retention expires. SQLite effects remain idempotent independently of the Restate journal.'},
  {name: 'PaperPortfolio', label: 'Serialize capital admission', role: 'Keyed virtual object', file: 'ledger.py',
    description: 'One serialized risk gate per fictional portfolio checks available cash and contract, event, correlated, total and realized-loss caps. Concurrent candidates cannot spend the same capital.',
    journal: 'Reservations and ledger writes use independent transaction and decision keys. Existing effects are reconciled before replay admission checks.',
    guard: 'A portfolio gate handles meaningful decisions; selected high-frequency market updates stay outside Restate. Stale data or failed reconciliation pauses execution.'},
];

function restate() {
  const container = node('div', 'view-content');
  append(container, append(node('div', 'restate-intro'), append(node('div'), node('p', 'eyebrow', 'DURABILITY AT THE DECISION BOUNDARY'), node('h2', '', 'Four durable responsibilities.'), node('p', '', 'The public dashboard reads published reports. Restate and the Python worker run locally for bounded campaigns. Restate preserves orchestration; the ledger independently preserves accounting.')), badge(`Restate ${state.proof.runtime} · SDK ${state.proof.sdk}`, 'green')));
  const architecture = section('Follow a decision through the system', 'Select a component to see what it preserves and how replay behaves.');
  const flow = node('div', 'service-flow'); const explanation = node('div', 'service-detail');
  function selectService(index) {
    for (const [i, button] of [...flow.children].entries()) {button.classList.toggle('selected', i === index); button.setAttribute('aria-pressed', String(i === index));}
    const service = services[index];
    explanation.replaceChildren(badge(service.role, 'green'), node('h3', '', service.label), node('p', '', service.description),
      append(node('div', 'two-column small'), append(node('div'), node('h4', '', 'Durable work'), node('p', '', service.journal)), append(node('div'), node('h4', '', 'Replay and integrity'), node('p', '', service.guard))), link(`Inspect ${service.name} in source ↗`, `${source}/src/sgr/paper/${service.file}`, 'quiet-link'));
  }
  services.forEach((service, index) => {const button = append(node('button', 'service-node'), node('span', 'step-number', `0${index + 1}`), node('strong', '', service.name), node('small', '', service.role)); button.addEventListener('click', () => selectService(index)); append(flow, button);});
  append(architecture, flow, explanation); selectService(0); append(container, architecture);
  const recovery = state.proof.recovery;
  const evidence = section('A real interruption test', 'Synthetic data, actual worker and runtime interruption. Evidence from the executable native recovery scenario.');
  const timeline = node('ol', 'timeline');
  const steps = [
    ['Commit a fill, lose the reply', `${recovery.fill_attempts.length} fill operations committed. Each operation ran ${recovery.fill_attempts.join(' / ')} time(s). The injected failure occurs after commit.`],
    ['Interrupt worker and runtime', `Durable state persisted on disk. Offline accounting remained unchanged: ${recovery.offline_unchanged ? 'verified' : 'not verified'}. No unattended worker remained running.`],
    ['Restart and reconcile', `Lost-ack reconciliation ran ${recovery.fill_reconciliation_attempts.join(' / ')} times. Completed page and analysis operations each ran ${recovery.completed_page_attempts} and ${recovery.completed_analysis_attempts} time(s).`],
    ['Replay and settle once', `Replay left balances and catalog unchanged: ${recovery.replay_unchanged ? 'verified' : 'not verified'}. Duplicate final settlement applied once: ${recovery.settlement_once ? 'verified' : 'not verified'}. Accounting invariants: ${recovery.invariants ? 'passed' : 'failed'}.`],
  ];
  steps.forEach(([heading, detail], index) => append(timeline, append(node('li'), node('span', 'timeline-number', String(index + 1)), append(node('div'), node('h3', '', heading), node('p', '', detail)))));
  append(evidence, timeline, node('p', 'footnote', `Pause/resume test: checkpoint stable ${state.proof.pause_resume.checkpoint_stable ? '✓' : '✕'} · resumed to completion ${state.proof.pause_resume.resumed_completed ? '✓' : '✕'}. Synthetic settlement virtual time: ${timestamp(recovery.synthetic_virtual_settlement_time)}.`)); append(container, evidence);
  const stress = state.proof.stress;
  append(container, append(node('div', 'metrics'), fact('SYNTHETIC THROUGHPUT', `${stress.candidates_per_second.toFixed(1)} / sec`, `${count(stress.completed_candidates)} candidates in ${stress.seconds.toFixed(2)} seconds`), fact('WORKER PEAK RSS', `${(stress.worker_peak_rss_kib / 1024).toFixed(1)} MiB`, 'Sampled process-group memory'), fact('RUNTIME PEAK RSS', `${(stress.runtime_peak_rss_kib / 1024).toFixed(1)} MiB`, 'Sampled process-group memory'), fact('PUBLIC REQUESTS IN STRESS TEST', count(stress.requests), 'The workload is explicitly synthetic')));
  const grid = node('div', 'two-column');
  const benefit = section('Where Restate helped', 'Recovery at the expensive, meaningful operation boundaries.');
  append(benefit, node('p', '', 'Completed discovery and forecasting were preserved through interruption. Durable timers kept lifecycle deadlines coherent. Keyed admission serialized concurrent spending, while the ledger reconciled lost acknowledgements without duplicating fills.'), link('Read the implementation and recovery guide ↗', `${source}/docs/restate-in-this-app.md`, 'quiet-link'));
  const overhead = section('Reliability limits and operating costs', 'Recovery correctness is the primary Restate evaluation criterion.');
  append(overhead, node('p', '', 'Restate adds a server, worker, journals and retained workflow state. These tests verify crash recovery on one machine with persisted storage. Sustained reliability rates, recovery latency and machine/storage-loss or high-availability failover remain unmeasured. Forecast quality is evaluated separately.'), link('Research limitations and next gates ↗', `${source}/docs/paper-execution.md`, 'quiet-link'));
  append(grid, benefit, overhead); append(container, grid);
  const commands = section('Reproduce the research locally', 'Foreground, bounded runtime. No credentials required for public Polymarket screening.');
  append(commands, node('pre', '', '.venv/bin/python -m sgr.paper.runtime --seconds 120 --state .runs/restate-paper\n.venv/bin/python -m sgr.cli campaign start --help\n.venv/bin/python -m sgr.cli campaign status <campaign-id>\n.venv/bin/python -m sgr.cli campaign report <campaign-id>'), link('Complete setup and operating commands ↗', `${source}/README.md`, 'quiet-link'));
  append(container, commands); return container;
}

async function loadJSON(file) {
  const response = await fetch(`./data/${file}`, {cache: 'no-cache'});
  if (!response.ok) throw Error('Published research reports could not be loaded. Please reload or inspect the repository.');
  return response.json();
}
async function start() {
  const manifest = await loadJSON('index.json');
  if (manifest.schema_version !== 1 || !Array.isArray(manifest.campaigns) || manifest.campaigns.some(file => !/^[a-z0-9-]+\.json$/.test(file))) throw Error('Unsupported published report manifest.');
  const [campaigns, proof] = await Promise.all([Promise.all(manifest.campaigns.map(async filename => ({...await loadJSON(filename), filename}))), loadJSON('recovery-proof.json')]);
  if (campaigns.some(campaign => campaign.schema_version !== 1 || !['public', 'synthetic'].includes(campaign.mode)) || proof.schema_version !== 1) throw Error('Unsupported public evidence schema.');
  state.campaigns = campaigns; state.proof = proof;
  try {
    const response = await fetch('./build.json');
    if (response.ok) {
      const build = await response.json();
      if (/^[a-f0-9]{40}$/.test(build.source_commit)) {
        source = `${repository}/blob/${build.source_commit}`;
        $('.sidebar-bottom a:last-child').href = `${source}/README.md`;
        $('#footer-docs').href = `${source}/README.md`;
        $('#build-label').textContent = `Published build ${build.source_commit.slice(0, 7)} · read-only`;
      }
    }
  } catch { /* Build metadata is optional for local source previews. */ }
  const params = new URLSearchParams(location.search);
  state.selected = Math.max(0, campaigns.findIndex(campaign => campaign.public_id === params.get('campaign')));
  state.view = Object.hasOwn(viewNames, params.get('view')) ? params.get('view') : 'overview';
  $('#campaign-picker').replaceChildren(...campaigns.map((campaign, index) => {const option = node('option', '', title(campaign)); option.value = index; return option;}));
  $('#campaign-picker').disabled = false; render();
}
start().catch(error => {$('#view').replaceChildren(); $('#load-error').textContent = error.message; $('#load-error').hidden = false;});
