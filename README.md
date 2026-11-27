# 名刺帳 · Meishi

A personal Japanese business-card web app. Capture a batch on your phone, let Codex read it automatically, then review the results on your laptop. SQLite on your Mac stores the images, extracted text, and confirmed fields.

## Run on your computer

Install the [Codex CLI](https://learn.chatgpt.com/docs/codex/cli) and use the ChatGPT sign-in option in the same macOS account that runs Meishi. Check with `codex login status`. This uses the Codex allowance in your ChatGPT plan. Meishi does not use API keys. If Codex is installed outside your shell's path, set `MEISHI_CODEX_BIN` to its absolute executable path.

From this directory:

```sh
uv sync --locked
uv run python app.py
```

Open [http://127.0.0.1:8000/](http://127.0.0.1:8000/) and choose **名刺を取り込む**. You can select multiple images from the file picker. JPEG, PNG, WebP, and HEIC are accepted, with a limit of 12 MB per file and 20 photos per batch. Pressing **取り込む** saves the photos on the Mac and queues them for automatic reading through Codex. Each queued image is sent to OpenAI.

On the dashboard, **要確認** lists the results waiting for review. Open a card, edit the fields, and click **確認して保存**. For a failed reading or a previously uploaded unsaved card, open it and use **読み取りをやり直す** to send it to Codex again. Previously uploaded photos are not automatically reprocessed.

## Connect your phone privately

Live capture on a phone needs HTTPS. One option is [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve) on the computer running Meishi, with your phone connected to the same tailnet.

1. Stop the local app and restart it with a password. In zsh:

   ```zsh
   read -rs 'MEISHI_PASSWORD?Meishi password: '
   export MEISHI_PASSWORD
   uv run python app.py
   ```

2. In another terminal, run `tailscale serve 8000` and open the HTTPS URL it prints on your phone. Sign in as `meishi` with the password above. Keep both processes running while using the app. The same URL works from your laptop.
3. Tap **名刺を撮影**, allow camera access, and take one photo per card. Tap **まとめて確認** to check the batch, then **取り込む** to upload.

Keep the app bound to loopback and use one app process per database. Uploaded cards waiting to be read stay queued across restarts. A reading interrupted during processing can be retried from the dashboard.

## Back up your cards

SQLite's backup command includes the card images and works while the app is running. Choose a new filename for each backup and keep it somewhere private.

```sh
sqlite3 data/meishi.sqlite3 ".backup 'data/meishi-backup.sqlite3'"
```

To check a backup without touching your current data, set `MEISHI_DB=/absolute/path/to/backup.sqlite3` and `MEISHI_PORT=8001` when launching a separate instance. Use a copy of the backup if you intend to edit it.

## Submit and clean up local leads

Send `company`, `source_url`, `signal`, and `lead_type` (`buyer` or `partner`)
to `POST /api/leads` with the `X-Meishi: 1` header. The newest run is
selected by `submitted_at`, with the greatest run ID breaking ties. A
processing or successful run is reused. If the newest run failed, submitting
again or calling `POST /api/leads/runs/{run_id}/retry` for that run starts a
new attempt.

The canonical URL folds host case, a leading `www.`, HTTP and HTTPS, fragments,
and trailing slashes. Paths retain their case and use dot-segment normalization.
Percent escapes use uppercase hex, and only escapes for unreserved characters
are decoded. Query parameters keep their order, repeats, blank values, and
`ref`. The tracking keys removed are `utm_*`, `gclid`, `fbclid`, `yclid`,
`mc_cid`, and `mc_eid`. Port 80 is the HTTP default and port 443 is the HTTPS
default. An explicit HTTP port 443 keeps the HTTP scheme in the key so that
canonicalizing the key again preserves its port.

Successful redirects register their final URL for future submissions. If that
URL already belongs to another lead, the run records the redirect for cleanup.
The lead keeps its original company, signal, and lead type. Each run records
the fields that started it and stores reused submissions in its
`resubmissions` history. The submit response lists changed fields in
`differing_fields`.

On startup, the app adds missing columns and backfills keys. Duplicate keys
that block uniqueness produce a warning. To review duplicates and redirect
relationships in a local SQLite database, run:

```sh
python3 scripts/dedup_leads.py --db data/meishi.sqlite3
```

Add `--apply` to compute and apply a fresh plan in one write transaction.
Merged leads retain their URLs as aliases, and moved runs keep their original
source, canonical, and fetch URLs. Contact history moves to the surviving
lead, which keeps one entry per date, channel, and recipient. Apply also retries key backfill and index
creation after the merges.

## Lead status and contact history

Record outreach with `POST /api/leads/status`. Use a lead or run ID in
`record_id`, a `source_url`, or a `company` name. Matching uses the ID
first, then the canonical URL and its aliases, then the normalized company name. A company
with multiple leads resolves to its oldest lead by name. Supply an ID or URL
to target a particular page.

If no lead matches the company or URL, Meishi creates a lead marked
`created_via=status_update` without fetching a page or starting a run.
Status can be `new`, `contacted`, `replied`, `meeting`, `won`, `lost`,
`bounced`, or `unsubscribed`.
The date uses `YYYY-MM-DD` and the channel can be `email`, `form`, `phone`,
or another nonempty label. Repeating the same lead, date, channel, and
recipient returns the original log entry with `deduped: true`. Each call sets
the current status, and `last_contacted_at` records the most recent contact date.

```sh
curl -u "meishi:$MEISHI_PASSWORD" -H 'X-Meishi: 1' \
  -H 'Content-Type: application/json' \
  -d '{"company":"株式会社GOOYA","status":"contacted","date":"2026-11-26","channel":"email","to_address":"hello@example.com","note":"Sent an introduction"}' \
  http://127.0.0.1:8000/api/leads/status
```

Read history in ascending date order with `GET /api/leads/{lead_id}/contact-log`
or use `GET /api/leads/contact-log?company=GOOYA` for all leads at a company.

To expose this operation through an MCP connector, register
`mcp_tools.UPDATE_LEAD_STATUS_TOOL` and dispatch calls to
`mcp_tools.update_lead_status(args, base_url, password=...)`. Omit the password
for a local Meishi instance running without one.

## Cold-email CRM

Use `PATCH /api/leads/{lead_id}` to edit CRM fields. The status endpoint
accepts the same fields when recording a contact. Omitted fields keep
their values. Set optional text and date fields to `null` to clear them.

- `email` stores the address with its domain lowercased. Record the public
  evidence page in `email_source_url`, when you checked it in
  `email_checked_at`, and a short quote or note in `email_evidence`.
- `segment` and `batch_id` group outreach. Use a comma-separated string
  for `tags`.
- `sequence_step` is a nonnegative integer and defaults to 0.
  `next_followup_at` takes a `YYYY-MM-DD` date. Enter follow-ups manually
  for business days 4–5 and 10, up to two follow-ups. Contact history
  captures the sequence step and batch at the time of each entry.
- Record replies with `status: replied`, `channel: inbound`, and the
  reply's `subject` and `note`. Replies, meetings, wins, losses, bounces,
  and unsubscribes clear the next follow-up date.
- Set `do_not_contact` to `true` to block outreach for one lead.
  A bounce or unsubscribe also suppresses the affected email address.
  A blocked send or scheduling change returns HTTP 409. Replies, wins,
  losses, bounces, and unsubscribes can still be recorded.

Use `POST /api/suppressions` with `kind` (`email` or `domain`), `value`,
`reason`, and optional `note` and `source`. Suppression matches email
addresses case-insensitively and domains include their subdomains.
`GET /api/suppressions/check?address=hello%40example.com` returns
`{"suppressed": true}` for a match. `GET /api/suppressions` lists active
entries. `DELETE /api/suppressions/{id}` soft-removes a row, and
`GET /api/suppressions?include_removed=true` includes removed rows.

`GET /api/leads` returns lead and company fields, the suppression flag,
and the latest contact. `GET /api/leads/export.csv` exports the same
selection as UTF-8 CSV with an Excel-compatible BOM. Both endpoints
accept `status`, `segment`, `batch_id`, `due_on_or_before` (inclusive),
`include_suppressed` (defaults to false), `limit`, and `offset`.
These routes use the app's standard authentication. Write requests
also require `X-Meishi: 1`.

For MCP, register `LIST_LEADS_TOOL`, `EXPORT_LEADS_CSV_TOOL`,
`ADD_SUPPRESSION_TOOL`, and `CHECK_SUPPRESSION_TOOL` from `mcp_tools`.
Dispatch to the corresponding lowercase handler with the same
`args`, `base_url`, and optional `password` as `update_lead_status`.
The CSV handler returns decoded CSV text.
