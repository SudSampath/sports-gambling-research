export const money = value => new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD', maximumFractionDigits: 2}).format(Number(value));
export const count = value => new Intl.NumberFormat('en-US').format(value);
export const probability = value => value === null || value === undefined ? 'Unknown' : `${(Number(value) * 100).toFixed(1)}%`;
export const edge = value => value === null || value === undefined ? 'Unknown' : `${(Number(value) * 100).toFixed(2)} pp`;
export const words = value => value.replaceAll('_', ' ');
export const short = value => value.length > 28 ? `${value.slice(0, 17)}…${value.slice(-6)}` : value;
export const timestamp = value => value ? new Intl.DateTimeFormat('en-US', {dateStyle: 'medium', timeStyle: 'short', timeZone: 'UTC'}).format(new Date(value)) + ' UTC' : 'Not recorded';
export const inventory = campaign => campaign.positions.filter(position => Number(position.shares) > 0);
export const filterCandidates = (candidates, search, status) => candidates.filter(candidate =>
  (status === 'all' || (status === 'eligible' ? candidate.eligible : !candidate.eligible)) &&
  [candidate.id, candidate.contract, candidate.event, ...candidate.reasons].join(' ').toLowerCase().includes(search.toLowerCase().trim()));
export const title = campaign => campaign.mode === 'public' ? 'Public market screening' :
  campaign.public_id.includes('stress') ? `${count(campaign.completed_candidates)}-candidate stress test` :
    campaign.public_id.includes('recovery') ? 'Interruption & recovery test' : 'Synthetic research campaign';
