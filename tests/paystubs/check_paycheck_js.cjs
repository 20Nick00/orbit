// Run with Node directly; no packages or browser required.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '../..');

class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.attrs = {};
    this.events = {};
    this.style = {};
    this.classList = {add() {}, remove() {}};
  }
  setAttribute(key, value) { this.attrs[key] = value; }
  appendChild(child) { this.children.push(child); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(event, callback) { this.events[event] = callback; }
}

const host = new Element('div');
const steps = [
  {l: 'Gross', v: '100', total: true},
  {l: 'Tax refund', v: '50', total: false},
  {l: 'Employer reversal', v: '-50', total: false},
  {l: 'Net worth change', v: '-1500', total: true},
];
vm.runInNewContext(fs.readFileSync(path.join(root, 'app/static/paycheck.js'), 'utf8'), {
  document: {
    createElementNS: (_, tag) => new Element(tag),
    createElement: tag => new Element(tag),
    getElementById: id => id === 'wf' ? host : {textContent: JSON.stringify(steps)},
  },
});
const svg = host.children[0];
const labels = svg.children.filter(node => node.tag === 'text').map(node => node.textContent);
assert(labels.includes('+$50'));
assert(labels.includes('−$50'));
assert(labels.includes('−$1.5k'));
assert(!labels.some(label => label.includes('$-')));
const bars = svg.children.filter(node => node.tag === 'rect');
bars[1].events.pointerenter();
assert.equal(host.children[1].textContent, 'Tax refund +$50.00');
bars[2].events.focus();
assert.equal(host.children[1].textContent, 'Employer reversal −$50.00');

const reviewTemplate = fs.readFileSync(path.join(root, 'app/templates/paycheck_review.html'), 'utf8');
const reviewScript = reviewTemplate.match(/<script>\s*([\s\S]*?)<\/script>/)[1];
function review(amounts, groups) {
  const status = {textContent: ''};
  vm.runInNewContext(reviewScript, {
    document: {
      getElementById: id => id === 'pay-reconcile'
        ? status : {textContent: JSON.stringify(amounts)},
      querySelectorAll: () => groups.map(value => ({value, addEventListener() {}})),
    },
  });
  return status.textContent;
}
// 100 - 60 - (-50) = 90; the employer reversal doesn't change take-home pay.
assert.equal(review(['100', '60', '-50', '-50', '90'],
  ['gross', 'tax', 'tax', 'employer', 'net']), 'Adds up');
// The negative-deduction convention flips all deductions, but never the employer.
assert.equal(review(['100', '-10', '-20', '-50', '70'],
  ['gross', 'tax', 'benefit', 'employer', 'net']), 'Adds up');
// With no tax lines, use the remaining deductions to infer the sign convention.
assert.equal(review(['100', '-10', '90'], ['gross', 'pretax', 'net']), 'Adds up');
assert.equal(review(['100', '10', '80'], ['gross', 'tax', 'net']), 'Off by $10.00');
console.log('Paycheck JavaScript regression checks passed.');
