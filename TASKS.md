# Tasks



- [x] Session 0: scaffold
- [x] Session 1: core models and snapshots
- [/] Session 2: Plaid aggregator (built and tested with simulated Plaid responses; real Sandbox run still to do)
- [ ] Nick: Paste your Plaid Sandbox keys into `.env` (PLAID_CLIENT_ID and PLAID_SECRET), then tell Claude
- [ ] Run the real Plaid Sandbox tests (`ORBIT_RUN_SANDBOX=1`) and one real hosted Link connection once keys are in `.env`
- [ ] Holdings: an account that sells everything keeps showing its last holdings, because `core/detail.py` reads the last non-empty day
- [x] Session 3: file import and manual assets (tested on fake files; no real bank export tried yet)
- [ ] Nick: Import one real CSV or OFX from each bank and check the signs and balance
- [x] Count accounts added late in past closed months automatically, without freezing their figures
- [x] Session 4: dashboard
- [x] Session 5: account and type pages
- [x] Merge branch `orbit-account-pages` into main
- [x] Session 6: paystubs (watcher, IMAP fetcher, parser, mapping UI, Paycheck page, tests)
- [x] Merge branch `paystubs` into main
- [ ] Nick: Try one real paystub PDF on the Paycheck page
- [x] Session 7: property valuation
- [x] Session 8: trends and almanac
- [ ] Decide whether to split mortgage payments into interest and principal; today the principal shows up as market gain
- [/] Session 9: package and publish (built and tested; public release waits for Nick's go-ahead)
- [ ] Run the Tailscale steps in the README end to end on a real tailnet
- [ ] Nick: Confirm whether your payroll provider emails PDFs (enables the mailbox fetcher) or is portal-only (folder drop)
- [ ] Nick: Create a Plaid dashboard account and request Production access
- [ ] Nick: Sign up for a RentCast API key (free tier)
- [ ] Nick: Check "Orbit" trademark (class 36) and a domain before any public launch
