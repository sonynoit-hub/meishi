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
