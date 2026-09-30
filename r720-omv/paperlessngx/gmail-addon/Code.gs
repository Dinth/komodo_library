/**
 * Paperless for Gmail — Workspace add-on.
 *
 * Purpose:      Send Gmail attachments (or the whole email) to Paperless-ngx,
 *               from a button next to an open email or hourly from the
 *               "Paperless" / "Paperless/Shared" labels.
 * Ownership:    Uploads use ONE central token (Paperless user "gmail-uploader",
 *               add_document only — it cannot read anything). The sender's
 *               address comes from their own Gmail profile, is lowercased and
 *               sent as the "Submitted by" custom field; Paperless workflows
 *               ("Gmail add-on: owner <user>") hand the document to that user.
 *               "Shared" adds the Shared tag, which grants both users access.
 * Dependencies: Advanced Gmail service (v1). Script properties:
 *               PAPERLESS_URL, PAPERLESS_TOKEN, SUBMITTED_BY_FIELD_ID,
 *               SHARED_TAG_ID. See README.md.
 * Author:       AI (Claude), 2026-09-30.
 */

const LABEL_TODO = 'Paperless';
const LABEL_SHARED = 'Paperless/Shared';
const LABEL_DONE = 'Paperless/Done';
const LABEL_ERROR = 'Paperless/Error';

// Extensions Paperless consumes here (Tika + Gotenberg cover the office/eml ones).
const SUPPORTED_EXT = [
  'pdf', 'png', 'jpg', 'jpeg', 'tif', 'tiff', 'gif', 'webp', 'bmp', 'heic',
  'txt', 'csv', 'md', 'eml', 'rtf',
  'doc', 'docx', 'odt', 'xls', 'xlsx', 'ods', 'ppt', 'pptx', 'odp',
];
// Attached images smaller than this are almost always signature logos.
const MIN_IMAGE_BYTES = 20 * 1024;
const MAX_PER_RUN = 25;

// ---------------------------------------------------------------- config

/** Reads the admin-only script properties; throws if any is missing. */
function config_() {
  const p = PropertiesService.getScriptProperties().getProperties();
  ['PAPERLESS_URL', 'PAPERLESS_TOKEN', 'SUBMITTED_BY_FIELD_ID', 'SHARED_TAG_ID'].forEach(function (k) {
    if (!p[k]) throw new Error('Script property ' + k + ' is not set');
  });
  return {
    url: p.PAPERLESS_URL.replace(/\/+$/, ''),
    token: p.PAPERLESS_TOKEN,
    fieldId: p.SUBMITTED_BY_FIELD_ID,
    sharedTagId: p.SHARED_TAG_ID,
  };
}

/** The signed-in user's address, from Gmail itself — never user input. */
function myEmail_() {
  return Gmail.Users.getProfile('me').emailAddress.toLowerCase();
}

// ---------------------------------------------------------------- cards

/** Home card: who you are, auto-filing switch, manual run. */
function onHomepage() {
  const auto = hasTrigger_();
  const section = CardService.newCardSection()
    .addWidget(CardService.newTextParagraph().setText(
      'Signed in as <b>' + myEmail_() + '</b>.<br>Open an email to send it to Paperless.'))
    .addWidget(CardService.newDecoratedText()
      .setTopLabel('Auto-filing (hourly)')
      .setText('Label mail <b>' + LABEL_TODO + '</b> (private) or <b>' + LABEL_SHARED + '</b> (both of you)')
      .setWrapText(true)
      .setSwitchControl(CardService.newSwitch()
        .setFieldName('auto')
        .setValue('on')
        .setSelected(auto)
        .setOnChangeAction(CardService.newAction().setFunctionName('toggleAuto'))))
    .addWidget(CardService.newTextButton()
      .setText('Process labelled mail now')
      .setOnClickAction(CardService.newAction().setFunctionName('runNow')));
  return CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader().setTitle('Paperless'))
    .addSection(section)
    .build();
}

/** Contextual card for the open email: what would be sent + two buttons. */
function onGmailMessage(e) {
  const id = e.gmail.messageId;
  const items = collect_(id);
  const summary = items.attachments.length
    ? items.attachments.map(function (a) { return '• ' + a.name; }).join('<br>')
    : 'No supported attachments — the whole email will be sent.';
  const act = function (shared) {
    return CardService.newAction().setFunctionName('sendClicked')
      .setParameters({ id: id, shared: shared ? '1' : '' });
  };
  const section = CardService.newCardSection()
    .addWidget(CardService.newTextParagraph().setText(summary))
    .addWidget(CardService.newButtonSet()
      .addButton(CardService.newTextButton().setText('Send to Paperless')
        .setTextButtonStyle(CardService.TextButtonStyle.FILLED).setOnClickAction(act(false)))
      .addButton(CardService.newTextButton().setText('Send as Shared').setOnClickAction(act(true))));
  return CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader().setTitle(items.subject || '(no subject)'))
    .addSection(section)
    .build();
}

/** Button handler: upload the open message, report back as a toast. */
function sendClicked(e) {
  let text;
  try {
    const n = processMessage_(e.parameters.id, !!e.parameters.shared, myEmail_());
    text = 'Sent ' + n + ' file' + (n === 1 ? '' : 's') + ' to Paperless' + (e.parameters.shared ? ' (shared)' : '');
  } catch (err) {
    text = 'Paperless upload failed: ' + err.message;
  }
  return notify_(text);
}

/** Switch handler: create or remove this user's hourly trigger. */
function toggleAuto(e) {
  const on = e.formInput && e.formInput.auto === 'on';
  ScriptApp.getProjectTriggers()
    .filter(function (t) { return t.getHandlerFunction() === 'processLabelled'; })
    .forEach(function (t) { ScriptApp.deleteTrigger(t); });
  if (on) ScriptApp.newTrigger('processLabelled').timeBased().everyHours(1).create();
  return notify_(on ? 'Auto-filing on (hourly)' : 'Auto-filing off');
}

/** Button handler: run the label sweep immediately. */
function runNow() {
  const r = processLabelled();
  return notify_('Processed ' + r.ok + ' email(s)' + (r.failed ? ', ' + r.failed + ' failed (see ' + LABEL_ERROR + ')' : ''));
}

function notify_(text) {
  return CardService.newActionResponseBuilder()
    .setNotification(CardService.newNotification().setText(text))
    .build();
}

function hasTrigger_() {
  return ScriptApp.getProjectTriggers().some(function (t) { return t.getHandlerFunction() === 'processLabelled'; });
}

// ---------------------------------------------------------------- sweep

/**
 * Hourly trigger (and "Process now"): uploads every message carrying the
 * Paperless or Paperless/Shared label, then swaps it for Done or Error.
 * Labels are per MESSAGE (Gmail API), so a thread's older mails are untouched.
 */
function processLabelled() {
  const email = myEmail_();
  const ids = labelIds_();
  const result = { ok: 0, failed: 0 };
  [[ids.todo, false], [ids.shared, true]].forEach(function (pair) {
    const res = Gmail.Users.Messages.list('me', { labelIds: [pair[0]], maxResults: MAX_PER_RUN });
    (res.messages || []).forEach(function (m) {
      try {
        processMessage_(m.id, pair[1], email);
        result.ok++;
      } catch (err) {
        console.error('message ' + m.id + ': ' + err.message);
        Gmail.Users.Messages.modify({ addLabelIds: [ids.error], removeLabelIds: [pair[0]] }, 'me', m.id);
        result.failed++;
      }
    });
  });
  return result;
}

// ---------------------------------------------------------------- core

/**
 * Uploads one message's supported attachments (or the raw .eml when it has
 * none) and relabels it Done. Returns the number of files sent.
 */
function processMessage_(id, shared, email) {
  const cfg = config_();
  const items = collect_(id);
  const ids = labelIds_();
  const files = items.attachments.length
    ? items.attachments.map(function (a) {
        const data = Gmail.Users.Messages.Attachments.get('me', id, a.attachmentId).data;
        return { blob: Utilities.newBlob(bytes_(data), a.mimeType, a.name), title: stripExt_(a.name) };
      })
    : [emlBlob_(id, items.subject)];
  files.forEach(function (f) { upload_(cfg, f.blob, f.title, email, shared); });
  Gmail.Users.Messages.modify(
    { addLabelIds: [ids.done], removeLabelIds: [ids.todo, ids.shared, ids.error] }, 'me', id);
  return files.length;
}

/** Subject + list of uploadable attachment parts (walks nested MIME parts). */
function collect_(id) {
  const msg = Gmail.Users.Messages.get('me', id, { format: 'full' });
  const headers = msg.payload.headers || [];
  const subject = (headers.find(function (h) { return h.name.toLowerCase() === 'subject'; }) || {}).value || '';
  const out = [];
  (function walk(part) {
    if (part.filename && part.body && part.body.attachmentId) {
      const ext = (part.filename.split('.').pop() || '').toLowerCase();
      const isImage = /^image\//.test(part.mimeType);
      const disp = ((part.headers || []).find(function (h) { return h.name.toLowerCase() === 'content-disposition'; }) || {}).value || '';
      const inline = /^inline/i.test(disp);
      if (SUPPORTED_EXT.indexOf(ext) !== -1 && !(isImage && (inline || part.body.size < MIN_IMAGE_BYTES))) {
        out.push({ name: part.filename, mimeType: part.mimeType, attachmentId: part.body.attachmentId });
      }
    }
    (part.parts || []).forEach(walk);
  })(msg.payload);
  return { subject: subject, attachments: out };
}

/** The whole message as .eml — Paperless renders it (Tika/Gotenberg). */
function emlBlob_(id, subject) {
  const raw = Gmail.Users.Messages.get('me', id, { format: 'raw' }).raw;
  const name = (subject || 'email').replace(/[\\/:*?"<>|]+/g, ' ').trim().slice(0, 120) || 'email';
  return { blob: Utilities.newBlob(bytes_(raw), 'message/rfc822', name + '.eml'), title: name };
}

/**
 * The advanced Gmail service already decodes `data`/`raw` to a byte array;
 * only the REST API hands out base64url strings. Accept either.
 */
function bytes_(data) {
  return typeof data === 'string' ? Utilities.base64DecodeWebSafe(data) : data;
}

/** POST to /api/documents/post_document/; throws on anything but 200. */
function upload_(cfg, blob, title, email, shared) {
  const fields = {};
  fields[cfg.fieldId] = email;
  const payload = { document: blob, title: title, custom_fields: JSON.stringify(fields) };
  if (shared) payload.tags = cfg.sharedTagId;
  const res = UrlFetchApp.fetch(cfg.url + '/api/documents/post_document/', {
    method: 'post',
    headers: { Authorization: 'Token ' + cfg.token },
    payload: payload,
    muteHttpExceptions: true,
  });
  if (res.getResponseCode() !== 200) {
    throw new Error('HTTP ' + res.getResponseCode() + ': ' + res.getContentText().slice(0, 200));
  }
}

/** Label name -> id, creating the four labels on first use. */
function labelIds_() {
  const existing = {};
  (Gmail.Users.Labels.list('me').labels || []).forEach(function (l) { existing[l.name] = l.id; });
  const ensure = function (name) {
    return existing[name] || Gmail.Users.Labels.create(
      { name: name, labelListVisibility: 'labelShow', messageListVisibility: 'show' }, 'me').id;
  };
  // Parent first so Gmail nests the children under it.
  return { todo: ensure(LABEL_TODO), shared: ensure(LABEL_SHARED), done: ensure(LABEL_DONE), error: ensure(LABEL_ERROR) };
}

function stripExt_(name) {
  return name.replace(/\.[^.]+$/, '');
}

/**
 * Run once from the editor after setting the script properties: checks the
 * URL and token without uploading anything (the token may only add, so a
 * GET of the upload endpoint answers 405 = reachable + authenticated).
 */
function testConnection() {
  const cfg = config_();
  const res = UrlFetchApp.fetch(cfg.url + '/api/documents/post_document/', {
    headers: { Authorization: 'Token ' + cfg.token }, muteHttpExceptions: true,
  });
  console.log('HTTP ' + res.getResponseCode() + ' (405 = OK: reachable and token accepted; 401 = bad token)');
  console.log('Signed in as ' + myEmail_());
}
