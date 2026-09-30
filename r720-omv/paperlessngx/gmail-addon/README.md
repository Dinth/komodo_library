# Paperless for Gmail (Workspace add-on)

Adds a **Paperless** panel to Gmail (web + mobile) for everyone in the
`paperless-users` group:

- **Send to Paperless** / **Send as Shared** next to any open email — uploads
  the supported attachments, or the whole email as `.eml` when there are none.
- Optional **hourly auto-filing** of mail labelled `Paperless` (private) or
  `Paperless/Shared` (both of you). Done mail is relabelled `Paperless/Done`,
  failures `Paperless/Error`.

## How ownership works

One central Paperless token (user `gmail-uploader`, `add_document` only — it
cannot read anything, every GET is 403). The add-on sends the sender's address,
read from their own Gmail profile and lowercased, as custom field **Submitted
by** (id 2). Workflows **Gmail add-on: owner &lt;user&gt;** move the document
to that user. *Shared* adds tag **Shared** (id 35), which grants both users.

Adding a user = add them to the group + one Paperless workflow for their
address. Until that workflow exists their uploads stay owned by
`gmail-uploader` (visible to superusers only, not lost).

Gmail scope is `gmail.modify` (read + label, no permanent delete) — not
`https://mail.google.com/`, which the simpler `GmailApp` service would need.

## Cloudflare geo-block exception

`papers.wickhay.uk` is proxied by Cloudflare, whose custom rule **Limit
countries** blocks everything outside PL/GB. Apps Script calls come from
Google's US servers (AS396982), so the rule carries one exception — the upload
endpoint only, from Google only:

```
and not (http.host eq "papers.wickhay.uk"
         and http.request.uri.path eq "/api/documents/post_document/"
         and ip.src.asnum eq 396982)
```

Symptom without it: `testConnection` logs `HTTP 403` and Traefik never sees
the request (Cloudflare Security Events → *Custom rules / Limit countries*).

## Setup

### 1. Apps Script project
1. <https://script.google.com> → **New project**, name it `Paperless for Gmail`.
2. **Project Settings** → tick *Show "appsscript.json" manifest file in editor*.
3. Replace `appsscript.json` and `Code.gs` with the files in this folder.
4. **Services** (+) → add **Gmail API** (identifier `Gmail`, v1). The manifest
   already declares it; this just makes the editor agree.
5. **Project Settings → Script properties**:

   | Property | Value |
   |---|---|
   | `PAPERLESS_URL` | `https://papers.wickhay.uk` |
   | `PAPERLESS_TOKEN` | output of `ssh root@10.10.1.13 cat /root/paperless-gmail-uploader.token` |
   | `SUBMITTED_BY_FIELD_ID` | `2` |
   | `SHARED_TAG_ID` | `35` |

6. Run **testConnection** from the editor and approve the consent screen.
   The log must say `HTTP 405` (reachable + token accepted). `401` = wrong
   token. It also prints the address uploads will be attributed to.

### 2. Try it on yourself
**Deploy → Test deployments → Install** (Google Workspace add-on). Reload
Gmail, open an email with a PDF, use the Paperless panel on the right. The
document should appear in Paperless owned by you, tagged *Inbox*.

### 3. Google Cloud project (needed to publish inside the domain)
1. <https://console.cloud.google.com> → new project `paperless-gmail-addon`.
2. **APIs & Services → OAuth consent screen** → User type **Internal**, app
   name `Paperless`, support + developer email yours. Add the five scopes from
   `appsscript.json` → `oauthScopes`.
3. Enable **Gmail API** and **Google Workspace Marketplace SDK**.
4. Copy the **project number** (Dashboard). Back in Apps Script:
   **Project Settings → Google Cloud Platform (GCP) Project → Change project** →
   paste the number.

### 4. Versioned deployment
Apps Script → **Deploy → New deployment** → type **Add-on** → description
`v1` → Deploy. Copy the **Deployment ID**.

### 5. Marketplace SDK (private listing)
Cloud console → **Google Workspace Marketplace SDK**:
- **App Configuration**: visibility **Private**; installation settings
  **Admin Only install**; app integration **Google Workspace add-on** →
  *Deploy using Apps Script deployment ID* → paste the Deployment ID; OAuth
  scopes = the same five; developer name/links (any of your pages).
- **Store Listing**: name, short description, the icon, category
  *Productivity* → **Publish**. Private listings go live immediately, no review.

### 6. Install for the group
1. Admin console → **Directory → Groups** → create `paperless-users@…`,
   add michal and paulina.
2. **Apps → Google Workspace Marketplace apps → Apps list → Add app → Add
   internal app** → *Paperless* → **Admin install** → *Selected groups* →
   `paperless-users`. Accept the scopes on everyone's behalf.
3. It appears in Gmail within minutes to a few hours.

### 7. Each person, once (optional)
Open Gmail → Paperless panel with no email open → switch on **Auto-filing
(hourly)**. Google does not let an admin install create triggers for users.
Then e.g. a Gmail filter `from:(@octopus.energy)` → *Apply label: Paperless*.

## Updating

Edit the code → **Deploy → Manage deployments** → edit the add-on deployment
→ **New version**. Everyone picks it up; no reinstall.

## Checks

- Home card shows **Signed in as …**. That exact (lowercased) address must match
  a Paperless workflow. If Paulina's primary Workspace address is not
  `paulina@piorkowska.net`, the workflow needs changing.
- Supported: pdf, images, txt/csv/md/rtf, eml, Office/ODF docs. Images under
  20 KB and inline images are skipped (signature logos).
