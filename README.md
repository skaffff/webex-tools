# Webex Tools

A local control panel with two tools:

- **Export space** — save an offline HTML conversation, original message JSON, and optional attachments.
- **Copy members** — create a separate space with selected colleagues and an optional client, leaving the original conversation unchanged.

Switch tabs to use the tool you need. Each tab has its own space selector. Tasks continue if you switch tabs.

Exporting is read-only. The optional **Copy members to a new space** feature creates a separate space and adds the people you review. Neither feature changes the source space or sends chat messages.

## What you need

- Python 3.10 or newer (`python3 --version` to check).
- A Webex account with access to the space you want to export.
- A Webex personal access token (instructions below).

No Python packages to install. The included double-click launcher is for macOS; you can also start the app from a terminal.

## 1. Download the project

On GitHub, choose **Code → Download ZIP**, then unzip it. Or use Git:

```sh
git clone https://github.com/skaffff/webex-tools.git
cd webex-tools
```

Run the following commands from inside the project folder.

## 2. Get your Webex API key

Webex calls this a **Personal Access Token**.

1. Open the [Webex Developer Portal](https://developer.webex.com/docs/getting-started).
2. Sign in with the same account you use in the Webex app.
3. Find **Your Personal Access Token** and click the copy icon.
4. Confirm the copy dialog if prompted.

The token expires **12 hours after signing in**. The warning about not using it in production apps is expected: this token is intended for short-lived API use. For a one-time export, finish the download while it is valid. Ongoing integrations should use OAuth instead. See [Webex authentication guidance](https://developer.webex.com/admin/docs/getting-started).

Your downloaded backup remains usable after the token expires. Keep the token private: do not put it in a GitHub issue, commit, or screenshot.

## 3. Save the token locally

Create your local settings file:

```sh
cp .env.example .env
```

Open `.env` in a text editor and replace the placeholder:

```dotenv
WEBEX_BEARER_TOKEN=paste_your_personal_access_token_here
```

Save the file beside `app.py`. You do not need to include the word `Bearer`.

`.env` and the entire `exports/` folder are excluded from Git. `.env.example` contains only a placeholder and is safe to share.

## 4. Start the controls

On macOS, double-click **start.command**. Or run:

```sh
python3 app.py
```

Keep that terminal window open, then open **[http://127.0.0.1:8766](http://127.0.0.1:8766)** in your browser.

If the launcher is not executable, run `chmod +x start.command`, or use the Python command above.

## 5. Export a space

1. Open the **Export space** tab and click **Load spaces**.
2. Filter by name and select the space or direct conversation.
3. Leave **Download attached files and images** checked if you want those files included.
4. Click **Export selected space**.
5. Watch the message and file counters. Large histories can take a while.
6. Wait for **Export complete**, then click **Open offline transcript**.

You can cancel an export. Cancelled or failed runs keep their partial data and are marked accordingly. A new export starts in a fresh folder; it does not resume a previous run.

## Copy members to a new space

Use this to create a separate client space while keeping your internal conversation intact.

1. Open the **Copy members** tab, click **Load spaces**, and select the internal space.
2. Click **Load selected space members**.
3. Enter a name for the new space and review the member checkboxes. Your own account is included automatically as the creator.
4. Optionally enter the client's Webex email address.
5. Click **Preview new space**. Review the exact name and recipients.
6. Click **Create space and add these people**. This is the step that changes Webex; members may receive Webex notifications.
7. Check the result for each person. If any addition fails, the new space is kept and the result explains the failure. Add missing people manually in Webex after resolving the issue.

The new space is standalone: it does not inherit the internal team, moderator roles, or other space settings. No conversation history or files are copied. Your organization must allow external communication with the client's domain.

The app does not automatically retry writes after a timeout, because the action may already have succeeded. Repeated submissions of the same preview reuse the same operation. If a result is uncertain or the server stops during creation, check Webex before starting a new preview. Previews expire after 30 minutes. Local operation records are kept in `.space-copy/`, which is ignored by Git, and the browser remembers the latest operation across refreshes.

## Where is the backup?

Each run creates a unique folder inside `exports/`:

```text
exports/
  Space name-YYYYMMDD-HHMMSS-unique-id/
    index.html        Readable offline conversation
    messages.json     Original message data
    room.json         Space details
    files.json        Message-to-file mapping
    manifest.json     Completion status and missing-file warnings
    attachments/      Downloaded files, when requested
```

Open `index.html` in a browser, even after stopping the exporter. **Keep the entire folder together** so attachment links keep working.

Transcript timestamps use **Europe/Prague**: CET in winter and CEST in summer. Original JSON timestamps remain unchanged. Replies link to the parent message when it is present in the export.

Before archiving a space, check that the export completed and review any warnings. `manifest.json` records missing attachments and the final status. Failed or cancelled exports may also contain a partial JSONL journal.

## Troubleshooting

| Problem | What to do |
| --- | --- |
| Webex denied access | Obtain a fresh personal access token, replace it in `.env`, and retry. Also check that you still have access to the space. |
| Missing token | Check that the filename is exactly `.env`, it is beside `app.py`, and the variable is named `WEBEX_BEARER_TOKEN`. |
| Port already in use | Run `WEBEX_EXPORT_PORT=8767 python3 app.py`, then open `http://127.0.0.1:8767`. |
| Some attachments are missing | Read `manifest.json`. A file may be unavailable, require different permissions, or use an unsupported download host. |
| Local server unavailable | Start `python3 app.py` again and keep its terminal window open. |
| Python command not found or version too old | Install a current Python 3 release from [python.org](https://www.python.org/downloads/). |

To stop the server, press **Control-C** in its terminal. The token is read again when loading spaces or starting a new export, so replacing it does not require a restart.

## Scope and privacy

- Exports contain only messages currently available to your account. Deleted or retention-expired messages cannot be recovered.
- This is an offline copy, not a format for restoring a space into Webex or a complete compliance archive.
- External cloud documents, whiteboards, meeting recordings, reactions, and edit history are not separately downloaded. Message fields returned by Webex remain in JSON.
- Pagination follows Webex's next-page links. Temporary API failures and rate limits are retried.
- The server listens only on your computer at `127.0.0.1`. It reads the token on the server and does not send it to the browser interface.
- Cross-site export requests are blocked. Download redirects remove authorization when the destination host changes. Unexpected initial download hosts are reported as missing files.
- Conversation text is escaped before rendering as HTML.
- Keep `.env` and your exported conversations private. Git ignores both, but they are still files on your computer.

## Project files

- `app.py` — local server and Webex export logic.
- `space_copy.py` — member preview and new-space creation.
- `test_space_copy.py` — mocked tests; run `python3 -m unittest -v`.
- `ui.html` — control panel.
- `start.command` — macOS launcher.
- `.env.example` — token configuration template.

Independent utility; not an official Cisco product.
