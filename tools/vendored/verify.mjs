#!/usr/bin/env node
/**
 * verify.mjs — INDEPENDENT receipt verifier. Deliberately standalone:
 * imports NOTHING from src/ — reimplements canonical JSON, PAE and the
 * doctrine checks so it can audit the engine rather than trust it.
 *
 * Usage:
 *   node verify/verify.mjs <receipt.json> [more.json ...]
 *   node verify/verify.mjs --dir receipts/
 *   node verify/verify.mjs --pubkey keys/engine_pubkey.json <receipt.json>
 *   node verify/verify.mjs --chain ledger/   — walk the hash chain: DSSE per
 *     link, seq contiguity, prev-pointer byte-hash linkage, exactly-once dir
 *     coverage, per-file sha256 equality (rewrites/deletions become loud)
 *   node verify/verify.mjs --book ledger/    — REPLAY the stateful paper book:
 *     DSSE per link, prev-pointer linkage, then recompute every fill, state
 *     and mark from the signed signal receipts alone (frozen v1 rules) and
 *     require byte-exact agreement — a book that can't be replayed FAILS
 *
 * Checks per receipt (ALL must pass):
 *   1. DSSE envelope: payloadType, base64 payload, ed25519 signature over
 *      spec-exact PAE("DSSEv1", type, payload) with keyid = sha256(SPKI)[:16].
 *   2. If --pubkey given: envelope key MATCHES the pinned key (else TOFU note).
 *   3. in-toto Statement shape; subject digest = sha256(canonical(decision)).
 *   4. Doctrine: posture ADVISORY_PAPER_ONLY + provenTrust===false;
 *      trustCeiling === 0.97 and no conviction/confidence exceeds it;
 *      locked-proven set EXACTLY {F1,F4,F7,F11,F12,F18,F19,F22};
 *      Λ status mentions Conjecture ("Conjecture 1"), never claims theorem;
 *      a BLOCKED verdict must list ≥1 blocking gate with a reason;
 *      every gate verdict ∈ {ALLOWED, BLOCKED}.
 * Exit code 0 iff every file verifies. Honest per-check output.
 */
import { createPublicKey, createHash, verify as edVerify } from 'node:crypto';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

const LOCKED = ['F1', 'F11', 'F12', 'F18', 'F19', 'F22', 'F4', 'F7']; // sorted
const CEILING = 0.97;

function canonicalize(v) {
  if (v === null || typeof v === 'number' || typeof v === 'boolean') {
    if (typeof v === 'number' && !Number.isFinite(v)) throw new Error('non-finite');
    return JSON.stringify(v);
  }
  if (typeof v === 'string') return JSON.stringify(v);
  if (Array.isArray(v)) return `[${v.map(canonicalize).join(',')}]`;
  if (typeof v === 'object') {
    return `{${Object.keys(v).sort().filter((k) => v[k] !== undefined)
      .map((k) => `${JSON.stringify(k)}:${canonicalize(v[k])}`).join(',')}}`;
  }
  throw new Error(`bad type ${typeof v}`);
}
const sha256Hex = (buf) => createHash('sha256').update(buf).digest('hex');
const pae = (t, body) => Buffer.concat([Buffer.from(`DSSEv1 ${Buffer.byteLength(t)} ${t} ${body.length} `, 'utf8'), body]);

function* walkConvictions(node, path = '$') {
  if (node && typeof node === 'object') {
    for (const [k, v] of Object.entries(node)) {
      if ((k === 'conviction' || k === 'confidence' || k === 'trust') && typeof v === 'number') yield [path + '.' + k, v];
      yield* walkConvictions(v, `${path}.${k}`);
    }
  }
}

function verifyFile(file, pinnedKey) {
  const fails = [];
  const notes = [];
  let env;
  try { env = JSON.parse(readFileSync(file, 'utf8')); } catch (e) { return { file, ok: false, fails: [`unreadable JSON: ${e.message}`] }; }
  if (env.payloadType !== 'application/vnd.in-toto+json') fails.push(`payloadType ${env.payloadType}`);
  let payloadBytes;
  try { payloadBytes = Buffer.from(env.payload, 'base64'); } catch { fails.push('payload not base64'); }
  let pub = pinnedKey;
  if (!pub && env.publicKeySpkiBase64) {
    pub = createPublicKey({ key: Buffer.from(env.publicKeySpkiBase64, 'base64'), type: 'spki', format: 'der' });
    notes.push('TOFU: verified against envelope-embedded key (pin with --pubkey for stronger trust)');
  }
  if (!pub) { fails.push('no public key'); return { file, ok: false, fails, notes }; }
  const keyid = sha256Hex(pub.export({ type: 'spki', format: 'der' })).slice(0, 16);
  const sigEntry = (env.signatures ?? []).find((s) => s.keyid === keyid);
  if (!sigEntry) fails.push(`no signature for keyid ${keyid}`);
  if (sigEntry && payloadBytes) {
    const ok = edVerify(null, pae(env.payloadType, payloadBytes), pub, Buffer.from(sigEntry.sig, 'base64'));
    if (!ok) fails.push('ed25519 signature INVALID over PAE');
  }
  let st = null;
  if (payloadBytes) {
    try { st = JSON.parse(payloadBytes.toString('utf8')); } catch { fails.push('payload not JSON'); }
  }
  if (st) {
    if (st._type !== 'https://in-toto.io/Statement/v1') fails.push(`statement _type ${st._type}`);
    const d = st.predicate?._doctrine;
    if (!d) fails.push('missing _doctrine block');
    else {
      if (d.posture?.mode !== 'ADVISORY_PAPER_ONLY') fails.push('posture not ADVISORY_PAPER_ONLY');
      if (d.posture?.provenTrust !== false) fails.push('provenTrust not locked false');
      if (d.trustCeiling !== CEILING) fails.push(`trustCeiling ${d.trustCeiling} ≠ ${CEILING}`);
      const locked = [...(d.lockedProvenFormulaIds ?? [])].sort();
      if (JSON.stringify(locked) !== JSON.stringify(LOCKED)) fails.push('locked-proven set not EXACTLY the canonical 8');
      const lam = String(d.lambdaStatus ?? '');
      if (!/conjecture 1/i.test(lam)) fails.push('Λ status missing "Conjecture 1"');
      if (/theorem/i.test(lam)) fails.push('Λ status claims "theorem" — doctrine violation');
    }
    for (const [p, v] of walkConvictions(st.predicate)) {
      if (v > CEILING) fails.push(`${p} = ${v} exceeds trust ceiling ${CEILING}`);
    }
    const dec = st.predicate?.decision ?? st.predicate?.summary ?? null;
    if (dec && st.subject?.[0]?.digest?.sha256) {
      const recomputed = sha256Hex(Buffer.from(canonicalize(dec), 'utf8'));
      if (recomputed !== st.subject[0].digest.sha256) fails.push('subject digest ≠ sha256(canonical(decision))');
    } else if (!st.subject?.[0]?.digest?.sha256) fails.push('missing subject digest');
    const gates = dec?.gates ?? [];
    for (const g of gates) {
      if (g.verdict !== 'ALLOWED' && g.verdict !== 'BLOCKED') fails.push(`gate ${g.gate} has non-canon verdict ${g.verdict}`);
    }
    if (dec?.verdict === 'BLOCKED') {
      const blocking = gates.filter((g) => g.verdict === 'BLOCKED');
      if (blocking.length === 0) fails.push('verdict BLOCKED but no blocking gate listed');
      if (blocking.some((g) => !g.reason)) fails.push('blocking gate missing honest reason');
    }
  }
  return { file, ok: fails.length === 0, fails, notes, keyid };
}

// ---- chain walk (self-contained; trusts only the pinned key + disk) ----
const CHAIN_RE = /^chain_\d{4}\.receipt\.json$/;

function verifyChain(ledgerDir, pinnedKey) {
  let dirs;
  try {
    dirs = readdirSync(ledgerDir, { withFileTypes: true }).filter((e) => e.isDirectory()).map((e) => e.name).sort();
  } catch (e) { console.log(`CHAIN FAIL: cannot read ${ledgerDir}: ${e.message}`); return false; }
  const problems = [];
  const links = [];
  const dirFiles = {};
  for (const d of dirs) {
    const names = readdirSync(join(ledgerDir, d), { withFileTypes: true }).filter((e) => e.isFile()).map((e) => e.name).sort();
    dirFiles[d] = names.filter((n) => n.endsWith('.json') && !CHAIN_RE.test(n));
    const chainFiles = names.filter((n) => CHAIN_RE.test(n));
    if (chainFiles.length > 1) problems.push(`${d}: ${chainFiles.length} chain receipts in one dir (ambiguous)`);
    for (const cf of chainFiles) {
      const full = join(ledgerDir, d, cf);
      const r = verifyFile(full, pinnedKey);
      if (!r.ok) problems.push(`${d}/${cf}: link receipt fails verification: ${r.fails.join('; ')}`);
      let body = null;
      try { body = JSON.parse(Buffer.from(JSON.parse(readFileSync(full, 'utf8')).payload, 'base64').toString('utf8'))?.predicate?.summary ?? null; } catch { /* handled below */ }
      if (!body || !Number.isInteger(body.seq)) { problems.push(`${d}/${cf}: unreadable chain body`); continue; }
      links.push({ dir: d, file: cf, body, sha256: sha256Hex(readFileSync(full)) });
    }
  }
  if (links.length === 0) { console.log('CHAIN FAIL: no chain receipts found under ' + ledgerDir); return false; }
  links.sort((a, b) => a.body.seq - b.body.seq);
  links.forEach((l, i) => { if (l.body.seq !== i + 1) problems.push(`seq not contiguous: expected ${i + 1}, found ${l.body.seq} (${l.dir}/${l.file})`); });
  if (links[0] && links[0].body.prev !== null) problems.push(`first link (${links[0].dir}/${links[0].file}) is not genesis: prev ≠ null`);
  for (let i = 1; i < links.length; i++) {
    const prev = links[i].body.prev;
    if (!prev) { problems.push(`seq ${links[i].body.seq}: missing prev pointer`); continue; }
    if (prev.sha256 !== links[i - 1].sha256 || prev.file !== links[i - 1].file || prev.runDir !== links[i - 1].dir) {
      problems.push(`seq ${links[i].body.seq}: prev pointer mismatch — chain BROKEN between ${links[i - 1].dir} and ${links[i].dir}`);
    }
  }
  const covered = new Map();
  for (const l of links) {
    for (const cov of l.body.covers ?? []) {
      if (covered.has(cov.dir)) problems.push(`dir ${cov.dir} sealed twice (seq ${covered.get(cov.dir).seq} and ${l.body.seq})`);
      else covered.set(cov.dir, { files: cov.files ?? [], seq: l.body.seq });
    }
  }
  for (const d of dirs) if (!covered.has(d)) problems.push(`dir ${d} NOT sealed by any chain link`);
  for (const [d, cov] of covered) {
    if (!dirs.includes(d)) { problems.push(`sealed dir ${d} MISSING from ledger (deleted after sealing)`); continue; }
    const onDisk = dirFiles[d];
    const listed = cov.files.map((f) => f.name).sort();
    if (JSON.stringify(onDisk) !== JSON.stringify(listed)) problems.push(`dir ${d}: file set mismatch — on disk [${onDisk.join(', ')}] vs sealed [${listed.join(', ')}]`);
    for (const f of cov.files) {
      if (!onDisk.includes(f.name)) continue;
      if (sha256Hex(readFileSync(join(ledgerDir, d, f.name))) !== f.sha256) problems.push(`${d}/${f.name}: sha256 mismatch — file REWRITTEN after sealing`);
    }
  }
  for (const p of problems) console.log(`      CHAIN FAIL: ${p}`);
  const head = links[links.length - 1];
  if (problems.length === 0) {
    console.log(`CHAIN OK  links=${links.length}  head=seq ${head.body.seq} sha256=${head.sha256.slice(0, 12)}…  dirs sealed=${covered.size}  files sealed=${[...covered.values()].reduce((a, c) => a + c.files.length, 0)}`);
    console.log('      note: honest limit — wholesale deletion of the newest link(s) needs external witnesses (Actions logs, INDEX git history)');
    return true;
  }
  console.log('CHAIN BROKEN');
  return false;
}

// ---- book replay (self-contained; reimplements the frozen v1 book rules) ----
const BOOK_RE = /^book_\d+\.receipt\.json$/;
const MICRO_B = 1_000_000n;
const QTY_B = 1_000_000_000n;

function toMicroB(x) {
  if (!Number.isFinite(x)) throw new Error('non-finite amount');
  const s = x.toFixed(6);
  const neg = s.startsWith('-');
  const [ints, fracs] = (neg ? s.slice(1) : s).split('.');
  const v = BigInt(ints) * MICRO_B + BigInt(fracs);
  return neg ? -v : v;
}
function microStrB(m) {
  const neg = m < 0n;
  const a = neg ? -m : m;
  return `${neg ? '-' : ''}${a / MICRO_B}.${(a % MICRO_B).toString().padStart(6, '0')}`;
}
function decisionFromStatement(file, st) {
  const dec = st?.predicate?.decision;
  if (!dec?.asset?.symbol || !dec.proposedAction || !dec.verdict) return null;
  return { file, symbol: dec.asset.symbol, proposedAction: dec.proposedAction, verdict: dec.verdict, priceUsd: dec.snapshot?.priceUsd ?? null, observedAtIso: dec.snapshot?.observedAtIso ?? null };
}

/** Byte-exact mirror of the engine's frozen v1 transition (portfolio math included). */
function replayTransition({ startState, config, decisions, generatedAtIso }) {
  const costRate = (config.costModel.feeBps + config.costModel.slippageBps) / 10_000;
  const book = { cashMicro: startState.cashMicro, positions: {}, fills: [] };
  for (const [a, p] of Object.entries(startState.positions)) book.positions[a] = { qtyE9: p.qtyE9, costMicro: p.costMicro };
  const sorted = [...decisions].sort((a, b) => (a.symbol < b.symbol ? -1 : a.symbol > b.symbol ? 1 : 0));
  const prices = {};
  for (const d of sorted) if (d.priceUsd > 0) prices[d.symbol] = d.priceUsd;
  const equityNow = () => {
    let eq = book.cashMicro;
    for (const [asset, p] of Object.entries(book.positions)) {
      if (p.qtyE9 === 0n) continue;
      const price = prices[asset];
      if (!(price > 0)) return null;
      eq += (p.qtyE9 * toMicroB(price)) / QTY_B;
    }
    return eq;
  };
  const noActions = [];
  for (const d of sorted) {
    const pos = book.positions[d.symbol];
    const held = !!pos && pos.qtyE9 > 0n;
    if (d.verdict !== 'ALLOWED') { noActions.push({ asset: d.symbol, action: 'NONE', why: `decision ${d.verdict} — fail closed, the gates hold the book` }); continue; }
    if (d.proposedAction === 'ENTER_LONG') {
      if (held) { noActions.push({ asset: d.symbol, action: 'NONE', why: 'already long — no pyramiding in v1' }); continue; }
      if (!(d.priceUsd > 0)) { noActions.push({ asset: d.symbol, action: 'NONE', why: 'no observed price — cannot fill honestly (fail closed)' }); continue; }
      const eq = equityNow();
      if (eq === null) { noActions.push({ asset: d.symbol, action: 'NONE', why: 'an open position is unpriced — equity not computable, no new entries (fail closed)' }); continue; }
      const allocMicro = (eq * BigInt(config.entryFractionBps)) / 10_000n;
      if (allocMicro <= 0n || allocMicro > book.cashMicro) { noActions.push({ asset: d.symbol, action: 'SKIPPED_INSUFFICIENT_CASH', why: `entry needs ${microStrB(allocMicro)} USD but paper cash is ${microStrB(book.cashMicro)} — no leverage, honest skip` }); continue; }
      const price = d.priceUsd;
      const notionalMicro = toMicroB(Number(microStrB(allocMicro)));
      const effPrice = price * (1 + costRate);
      const qtyE9 = (notionalMicro * QTY_B) / toMicroB(effPrice);
      const grossQtyE9 = (notionalMicro * QTY_B) / toMicroB(price);
      const modeledCostMicro = ((grossQtyE9 - qtyE9) * toMicroB(price)) / QTY_B;
      const cur = book.positions[d.symbol] ?? { qtyE9: 0n, costMicro: 0n };
      book.cashMicro -= notionalMicro;
      cur.qtyE9 += qtyE9;
      cur.costMicro += notionalMicro;
      book.positions[d.symbol] = cur;
      book.fills.push({ asset: d.symbol, side: 'BUY', notionalUsd: microStrB(notionalMicro), price: String(price), effectivePrice: effPrice.toFixed(10), qtyE9: qtyE9.toString(), modeledCostUsd: microStrB(modeledCostMicro), costModel: { feeBps: config.costModel.feeBps, slippageBps: config.costModel.slippageBps, label: 'MODELED' }, atIso: d.observedAtIso, reason: 'ALLOWED ENTER_LONG' });
    } else if (d.proposedAction === 'EXIT_LONG') {
      if (!held) { noActions.push({ asset: d.symbol, action: 'NONE', why: 'no open position to exit' }); continue; }
      if (!(d.priceUsd > 0)) { noActions.push({ asset: d.symbol, action: 'NONE', why: 'no observed price — cannot exit honestly (fail closed, position remains)' }); continue; }
      const price = d.priceUsd;
      const q = book.positions[d.symbol].qtyE9;
      const effPrice = price * (1 - costRate);
      const proceedsMicro = (q * toMicroB(effPrice)) / QTY_B;
      const grossMicro = (q * toMicroB(price)) / QTY_B;
      book.cashMicro += proceedsMicro;
      book.positions[d.symbol].qtyE9 -= q;
      if (book.positions[d.symbol].qtyE9 === 0n) book.positions[d.symbol].costMicro = 0n;
      book.fills.push({ asset: d.symbol, side: 'SELL', notionalUsd: microStrB(proceedsMicro), price: String(price), effectivePrice: effPrice.toFixed(10), qtyE9: q.toString(), modeledCostUsd: microStrB(grossMicro - proceedsMicro), costModel: { feeBps: config.costModel.feeBps, slippageBps: config.costModel.slippageBps, label: 'MODELED' }, atIso: d.observedAtIso, reason: 'ALLOWED EXIT_LONG' });
    } else {
      noActions.push({ asset: d.symbol, action: 'NONE', why: `${d.proposedAction} — no book action` });
    }
  }
  // mark-to-market (mirror)
  const positionsOut = [];
  let equityMicro = book.cashMicro;
  let unpriced = 0;
  for (const [asset, pos] of Object.entries(book.positions)) {
    if (pos.qtyE9 === 0n) continue;
    const p = prices[asset];
    if (!(p > 0)) { positionsOut.push({ asset, qtyE9: pos.qtyE9.toString(), value: { label: 'UNAVAILABLE', note: 'no observed price at mark time' } }); unpriced++; continue; }
    const valueMicro = (pos.qtyE9 * toMicroB(p)) / QTY_B;
    equityMicro += valueMicro;
    positionsOut.push({ asset, qtyE9: pos.qtyE9.toString(), markPrice: String(p), valueUsd: microStrB(valueMicro) });
  }
  const mark = {
    atIso: generatedAtIso,
    cashUsd: microStrB(book.cashMicro),
    positions: positionsOut,
    equityUsd: unpriced === 0 ? microStrB(equityMicro) : null,
    equityNote: unpriced === 0 ? undefined : `${unpriced} position(s) unpriced — equity not computable (honest empty)`,
    fillsSoFar: book.fills.length,
  };
  const statePositions = {};
  for (const [asset, p] of Object.entries(book.positions)) {
    if (p.qtyE9 > 0n) statePositions[asset] = { qtyE9: p.qtyE9.toString(), costMicro: p.costMicro.toString() };
  }
  return { fills: book.fills, noActions, state: { cashMicro: book.cashMicro.toString(), positions: statePositions }, mark, sortedDecisions: sorted };
}

function verifyBook(ledgerDir, pinnedKey) {
  let dirs;
  try {
    dirs = readdirSync(ledgerDir, { withFileTypes: true }).filter((e) => e.isDirectory()).map((e) => e.name).sort();
  } catch (e) { console.log(`BOOK FAIL: cannot read ${ledgerDir}: ${e.message}`); return false; }
  const problems = [];
  const links = [];
  for (const d of dirs) {
    const names = readdirSync(join(ledgerDir, d), { withFileTypes: true }).filter((e) => e.isFile()).map((e) => e.name).sort();
    const bookFiles = names.filter((n) => BOOK_RE.test(n));
    if (bookFiles.length > 1) problems.push(`${d}: ${bookFiles.length} book receipts in one dir (ambiguous)`);
    for (const bf of bookFiles) {
      const full = join(ledgerDir, d, bf);
      const r = verifyFile(full, pinnedKey);
      if (!r.ok) problems.push(`${d}/${bf}: book receipt fails verification: ${r.fails.join('; ')}`);
      let body = null;
      try { body = JSON.parse(Buffer.from(JSON.parse(readFileSync(full, 'utf8')).payload, 'base64').toString('utf8'))?.predicate?.summary ?? null; } catch { /* handled below */ }
      if (!body || !Number.isInteger(body.seq)) { problems.push(`${d}/${bf}: unreadable book body`); continue; }
      links.push({ dir: d, file: bf, body, sha256: sha256Hex(readFileSync(full)) });
    }
  }
  if (links.length === 0) { console.log('BOOK FAIL: no book receipts found under ' + ledgerDir); return false; }
  links.sort((a, b) => a.body.seq - b.body.seq);
  links.forEach((l, i) => { if (l.body.seq !== i + 1) problems.push(`seq not contiguous: expected ${i + 1}, found ${l.body.seq} (${l.dir}/${l.file})`); });
  if (links[0] && links[0].body.prev !== null) problems.push(`first book (${links[0].dir}/${links[0].file}) is not genesis: prev ≠ null`);
  for (const l of links) if (l.body.runDir !== l.dir) problems.push(`seq ${l.body.seq}: body.runDir ${l.body.runDir} ≠ actual dir ${l.dir}`);
  for (let i = 1; i < links.length; i++) {
    const prev = links[i].body.prev;
    if (!prev) { problems.push(`seq ${links[i].body.seq}: missing prev pointer`); continue; }
    if (prev.sha256 !== links[i - 1].sha256 || prev.file !== links[i - 1].file || prev.runDir !== links[i - 1].dir) {
      problems.push(`seq ${links[i].body.seq}: prev pointer mismatch — book chain BROKEN between ${links[i - 1].dir} and ${links[i].dir}`);
    }
    if (!(links[i].dir > links[i - 1].dir)) problems.push(`seq ${links[i].body.seq}: run dir ${links[i].dir} not after ${links[i - 1].dir}`);
    if (canonicalize(links[i].body.config) !== canonicalize(links[i - 1].body.config)) problems.push(`seq ${links[i].body.seq}: config drifted from previous link (must be inherited unchanged)`);
  }
  // Declared gap honesty: pre-book and skipped dirs must match the disk exactly.
  if (links[0]) {
    const expectPre = dirs.filter((d) => d < links[0].dir);
    if (canonicalize(links[0].body.preBookRunDirs ?? null) !== canonicalize(expectPre)) problems.push(`genesis preBookRunDirs do not match the ledger (expected [${expectPre.join(', ')}])`);
  }
  for (let i = 1; i < links.length; i++) {
    const expectSkip = dirs.filter((d) => d > links[i - 1].dir && d < links[i].dir);
    if (canonicalize(links[i].body.skippedRunDirs ?? null) !== canonicalize(expectSkip)) problems.push(`seq ${links[i].body.seq}: skippedRunDirs do not match the ledger (expected [${expectSkip.join(', ')}])`);
  }
  // REPLAY each link from the signed signal receipts on disk.
  for (let i = 0; i < links.length; i++) {
    const l = links[i];
    const cfg = l.body.config;
    if (!cfg?.costModel || !Number.isFinite(cfg.costModel.feeBps) || !Number.isFinite(cfg.costModel.slippageBps) || !Number.isFinite(cfg.entryFractionBps) || !(cfg.startingCashUsd > 0)) {
      problems.push(`seq ${l.body.seq}: config incomplete — cannot replay`); continue;
    }
    if (!dirs.includes(l.dir)) { problems.push(`seq ${l.body.seq}: run dir ${l.dir} missing from ledger`); continue; }
    const sigNames = readdirSync(join(ledgerDir, l.dir), { withFileTypes: true }).filter((e) => e.isFile()).map((e) => e.name).filter((n) => n.startsWith('signal_') && n.endsWith('.receipt.json')).sort();
    const decisions = [];
    const excludedFiles = [];
    for (const n of sigNames) {
      const r = verifyFile(join(ledgerDir, l.dir, n), pinnedKey);
      if (!r.ok) { excludedFiles.push(n); continue; }
      let st = null;
      try { st = JSON.parse(Buffer.from(JSON.parse(readFileSync(join(ledgerDir, l.dir, n), 'utf8')).payload, 'base64').toString('utf8')); } catch { excludedFiles.push(n); continue; }
      const dec = decisionFromStatement(n, st);
      if (dec) decisions.push(dec);
    }
    const startState = l.body.seq === 1
      ? { cashMicro: toMicroB(cfg.startingCashUsd), positions: {} }
      : (() => {
          const prevBody = links[i - 1].body;
          const positions = {};
          for (const [a, p] of Object.entries(prevBody.state?.positions ?? {})) positions[a] = { qtyE9: BigInt(p.qtyE9), costMicro: BigInt(p.costMicro) };
          return { cashMicro: BigInt(prevBody.state.cashMicro), positions };
        })();
    let rep;
    try { rep = replayTransition({ startState, config: cfg, decisions, generatedAtIso: l.body.generatedAtIso }); }
    catch (e) { problems.push(`seq ${l.body.seq}: replay threw — ${e.message}`); continue; }
    const expectInputs = {
      signalFiles: rep.sortedDecisions.map((d) => d.file).sort(),
      decisions: rep.sortedDecisions.map(({ file, symbol, proposedAction, verdict, priceUsd, observedAtIso }) => ({ file, symbol, proposedAction, verdict, priceUsd, observedAtIso })),
      excludedSignals: { count: excludedFiles.length, files: [...excludedFiles].sort() },
    };
    if (canonicalize(l.body.inputs ?? null) !== canonicalize(expectInputs)) problems.push(`seq ${l.body.seq}: inputs do not match the verified signal receipts on disk — book misrepresents its inputs`);
    if (canonicalize(l.body.fills ?? null) !== canonicalize(rep.fills)) problems.push(`seq ${l.body.seq}: fills do not REPLAY from the signed decisions (recomputed fills differ)`);
    if (canonicalize(l.body.noActions ?? null) !== canonicalize(rep.noActions)) problems.push(`seq ${l.body.seq}: noActions do not replay (fail-closed notes differ)`);
    if (canonicalize(l.body.state ?? null) !== canonicalize(rep.state)) problems.push(`seq ${l.body.seq}: end state does not replay — cash/positions REWRITTEN or miscomputed`);
    if (canonicalize(l.body.mark ?? null) !== canonicalize(rep.mark)) problems.push(`seq ${l.body.seq}: mark-to-market does not replay — equity cannot be trusted`);
  }
  for (const p of problems) console.log(`      BOOK FAIL: ${p}`);
  const head = links[links.length - 1];
  if (problems.length === 0) {
    const eq = head.body.mark?.equityUsd;
    const usd = (v) => '\u0024' + v; // literal dollar sign, kept as escape to survive tooling
    console.log(`BOOK OK  links=${links.length}  head=seq ${head.body.seq}  equity=${eq === null || eq === undefined ? 'UNAVAILABLE (honest empty)' : usd(eq)} [MODELED]  open=${(head.body.mark?.positions ?? []).length}  cash=${usd(head.body.mark?.cashUsd)}`);
    console.log('      note: every fill, state and mark recomputed from DSSE-verified signal receipts alone — paper simulation, NOT real funds');
    return true;
  }
  console.log('BOOK BROKEN');
  return false;
}

// ---- main ----
const args = process.argv.slice(2);
let pinnedKey = null;
let chainDir = null;
let bookDir = null;
const files = [];
for (let i = 0; i < args.length; i++) {
  if (args[i] === '--pubkey') {
    const pj = JSON.parse(readFileSync(args[++i], 'utf8'));
    pinnedKey = createPublicKey({ key: Buffer.from(pj.publicKeySpkiBase64, 'base64'), type: 'spki', format: 'der' });
  } else if (args[i] === '--dir') {
    const dir = args[++i];
    for (const f of readdirSync(dir)) {
      if (f.endsWith('.json') && statSync(join(dir, f)).isFile()) files.push(join(dir, f));
    }
  } else if (args[i] === '--chain') {
    chainDir = args[++i];
  } else if (args[i] === '--book') {
    bookDir = args[++i];
  } else files.push(args[i]);
}
if (files.length === 0 && !chainDir && !bookDir) {
  console.error('usage: node verify/verify.mjs [--pubkey keys/engine_pubkey.json] (--dir receipts/ | --chain ledger/ | --book ledger/ | receipt.json ...)');
  process.exit(2);
}
let allOk = true;
for (const f of files.sort()) {
  const r = verifyFile(f, pinnedKey);
  allOk &&= r.ok;
  console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${f}${r.keyid ? `  keyid=${r.keyid}` : ''}`);
  for (const n of r.notes ?? []) console.log(`      note: ${n}`);
  for (const x of r.fails ?? []) console.log(`      FAIL: ${x}`);
}
if (files.length > 0) console.log(allOk ? `\nAll ${files.length} receipt(s) verified.` : '\nVERIFICATION FAILED');
if (chainDir) allOk = verifyChain(chainDir, pinnedKey) && allOk;
if (bookDir) allOk = verifyBook(bookDir, pinnedKey) && allOk;
process.exit(allOk ? 0 : 1);
