---
name: recon
description: External attack-surface reconnaissance with the `recon` toolbox container in the secops stack (subfinder, dnsx, alterx, naabu, httpx, tlsx, katana, nuclei-recon). Use when asked to map what an organisation or domain exposes to the internet and assess the associated risks, to enumerate subdomains, or to run a non-intrusive external scan. Discovery and fingerprinting only, never exploitation.
---

# External reconnaissance

Map what a target exposes to the internet and what risk that carries, without
attempting to break in. The tools live in the `recon` container on r720-omv
(`r720-omv/secops/compose.yaml`, image built from `r720-omv/secops/recon/`).

## Hard rules

1. **Scope before packets.** Do nothing active until the user has given the
   root domains and/or IP ranges and confirmed they own them or hold written
   authorisation. Write them to `scope.txt` (below) and treat that file as the
   only allow-list.
2. **No break-in attempts.** No brute-forcing, default-login checks, fuzzing,
   exploit templates or DoS. Use `nuclei-recon`, never plain `nuclei`, and do
   not override its tag lists. If the user wants more, that is a separate,
   explicit request.
3. **Third parties stay out.** Discovery returns hosts on CDNs, SaaS and
   shared hosting. Record them, but do not port-scan or template-scan anything
   whose IP is not the target's own. Use `-exclude-cdn` / `-cdn` to spot them.
4. **Be polite.** Keep the default rate limits; lower them for small or
   fragile targets. One engagement at a time.
5. **Findings are confidential and this repo is public.** Results stay under
   `/recon/engagements/` on the server. Never commit target names, findings or
   reports here.
6. **Vantage point.** From r720 the view of someone else's estate is a true
   outside view. The view of the home network is not: say so if asked to
   assess it from here.

## Running the tools

Everything runs as `docker exec recon <tool> ...` on r720-omv, using whatever
server access this session has. For pipelines, wrap in a shell:

```
docker exec recon sh -c 'cd /recon/engagements/<name> && <pipeline>'
```

If the container is missing, the secops stack has not been deployed since the
toolbox was added; ask the user to deploy it in Komodo.

- State persists in `/recon` (host: `/opt/docker/secops/recon`). The root
  filesystem is read-only; use `/recon` or `/tmp`.
- All tools emit JSON lines (`-json` / `-jsonl` / `-oJ`); `jq` is installed.
- Flags change between releases. Run `<tool> -h` before the first use of a
  tool in a session rather than trusting the examples below verbatim.
- `naabu` has no raw-socket privilege, so it does TCP connect scans.
- `subfinder` works without API keys but finds more with them. Keys live in
  `/recon/.config/subfinder/provider-config.yaml`, server-side only. If it is
  absent, tell the user coverage is reduced; do not create keys yourself.
- Refresh templates at the start of an engagement: `nuclei -ut`.

## Layout per engagement

```
/recon/engagements/<name>/
  scope.txt        # root domains and CIDRs the user authorised, one per line
  subs.txt         # every hostname discovered
  resolved.jsonl   # dnsx output
  inscope.txt      # hosts that resolve to in-scope, non-third-party IPs
  ports.jsonl      # naabu output
  http.jsonl       # httpx output
  tls.jsonl        # tlsx output
  nuclei.jsonl     # nuclei-recon output
  report.md        # the write-up
```

## Pipeline

Work through the stages in order and finish each before the next. Report
progress to the user at each stage boundary with counts.

1. **Passive subdomain discovery** (no traffic to the target):
   `subfinder -dL roots.txt -all -silent -o subs.txt`
   If the user can export the DNS zone, ask for it: it is authoritative, and
   discovery then shows what is *not* in it.
2. **Active DNS expansion** (queries go to public resolvers):
   - permutations: `alterx -l subs.txt -enrich -limit 20000 | dnsx -silent`
   - wordlist: `dnsx -d <root> -w <wordlist> -wd <root> -silent`
     (`-wd` filters wildcard DNS; without it a wildcard zone makes every guess
     a hit). No wordlist is bundled; fetch one into `/recon/wordlists/`.
   - one zone-transfer attempt per nameserver: `dig axfr <root> @<ns>`
3. **Resolve and classify:**
   `dnsx -l subs.txt -a -aaaa -cname -resp -json -o resolved.jsonl`
   Split into in-scope hosts, third-party hosts (by CNAME target, ASN, `whois`)
   and dangling records (CNAME to something that no longer exists: takeover
   candidates). Only in-scope hosts go forward.
4. **Ports:**
   `naabu -l inscope.txt -top-ports 1000 -exclude-cdn -rate 500 -json -o ports.jsonl`
5. **HTTP and TLS fingerprinting:**
   `httpx -l <host:port list> -title -tech-detect -cdn -tls-grab -favicon -json -o http.jsonl`
   `tlsx -l <host:port list> -san -tls-version -expired -self-signed -mismatched -json -o tls.jsonl`
   Feed new hostnames from certificate SANs back into stage 3 if in scope.
6. **Light crawl** (optional, only for the main web properties):
   `katana -list urls.txt -depth 2 -js-crawl -field-scope rdn -jsonl`
   to find linked hosts and endpoints. No form submission, no headless.
7. **Non-intrusive checks:**
   `nuclei-recon -list urls.txt -jsonl -o nuclei.jsonl`
8. **Mail and domain hygiene** per root domain: SPF, DMARC and MX via `dig`;
   registration and expiry via `whois`.

## Risk assessment

The scan output is raw material; the assessment is the deliverable.

- Build one inventory: host, IP, owner (target or third party), open ports,
  product and version, findings.
- Map detected product versions to CVEs. Prioritise by known exploitation
  (CISA KEV catalogue) and exploit likelihood (EPSS from FIRST), not by CVSS
  alone. A version match is a *possible* exposure: say so, since backported
  patches are invisible from outside.
- Flag exposure classes regardless of CVEs: admin panels and login pages
  reachable from the internet, remote access (RDP, SSH, VPN portals),
  databases and file shares, forgotten dev/staging hosts, expired or
  mismatched certificates, legacy TLS versions, dangling DNS, missing or
  permissive SPF/DMARC.
- State what was not tested and why (out of scope, third party, intrusive).

`report.md`: summary a non-technical reader can act on first, then findings
ordered by risk (what it is, why it matters, what to do), then the inventory,
then method, scope and limits. Give the user the report content in the
conversation; leave the file on the server.
