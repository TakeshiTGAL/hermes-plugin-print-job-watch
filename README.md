# print-job-watch

Hermes plugin that reads one printer, saves an optional webcam still, and can pause, resume, or cancel only after a person approves that call. A cron watch reports disconnects, errors, completion, pauses, cancels, and stalls. It does not move the printer.

This plugin can be used with OctoPrint. It is built on the OctoPrint REST API. This repository does not include OctoPrint. [OctoPrint is a registered trademark](https://octoprint.org) owned by Gina Häußge.

It does not upload files, start prints, set temperatures, send G-code, connect, or disconnect. It does not speak PrusaLink.

## First request

1. `hermes plugins install TakeshiTGAL/hermes-plugin-print-job-watch --enable`
2. Set `OCTOPRINT_URL`, such as `http://printer.example:5000`.
3. Set `OCTOPRINT_API_KEY` to a user API key. Leave access control on.
4. OctoPrint is already running.
5. Ask "what is the hotend temperature". The model calls `print_job_status` with no arguments. That call does not ask for approval. Pause uses `print_job_control` with action pause and asks once.

Temperature uses print_job_status with no arguments. 日本語: 温度は引数なしの print_job_status。一時停止は action を pause にして、承認は1回。

## What you can ask

| Tool | What it does |
| --- | --- |
| `print_job_status` | Job state, connection state, completion (percent), temperatures, and `printTimeLeft`. Optional still. |
| `print_job_control` | `pause`, `resume`, or `cancel`, each through Hermes approval. |
| `print_job_watch` | Compare with the previous sample. No arguments. Does not move the printer. A call outside cron does not update the saved watch. |

Slash command: `/print-job-watch status | watch`. It does not pause, resume, or cancel. Ask in the chat so the tool can request approval. `schedule` and `unschedule` are not on the slash command, so a chat user cannot create, replace, or remove the cron job.

CLI: `hermes print-job-watch status | watch | schedule | unschedule`. The CLI cannot pause, resume, or cancel.

## Setup

Hermes enables a plugin's tools on every platform, including gateways, until you turn a platform off with `hermes tools`. That switch does not remove the slash command. Slash access is per platform, and a room's `group_user_allowed_commands` can also apply to a direct message. Stop the slash command with `allow_admin_from`, `user_allowed_commands`, and `group_user_allowed_commands`, or disable the plugin. This plugin has no allowlist of its own. Anyone the gateway already admits (its allowlist, pairing, or allow-all) can ask the model to call these tools and can approve a move. A group-chat allowlist admits that chat; it does not check each person in the chat.

```
OCTOPRINT_URL=http://printer.example:5000
OCTOPRINT_API_KEY=your-user-api-key
# optional, same hostname as OCTOPRINT_URL, another port is fine
OCTOPRINT_SNAPSHOT_URL=http://printer.example:8080/?action=snapshot
```

`OCTOPRINT_URL` is one origin: scheme, host, and optional port. No path, query, user, or password. Plain `http` sends `X-Api-Key` unencrypted. The key is sent only as that header, never in the query string. An empty key is not sent. A proxy set in the environment is not used, so the API key is not sent to it. This plugin never calls `GET /api/settings`.

Each tool call and each slash command reads `OCTOPRINT_URL`, `OCTOPRINT_API_KEY`, and `OCTOPRINT_SNAPSHOT_URL` for the active Hermes profile through the Hermes secret scope, not from a value kept since startup. On a gateway that serves several profiles from one process, each profile uses its own printer, key, and still URL, and a slash command carries that profile's scope into its worker thread. With a single profile, the values set in the environment are used as before.

Create a user API key in OctoPrint. Do not paste the global key from settings.

OctoPrint's own documentation says that when access control is off, a missing or wrong key is treated as a full admin. Leave access control on.

State is stored under `<HERMES_HOME>/plugin-data/print-job-watch` (one directory per Hermes profile): job state, connection state, filename, completion, file position, print time left, and timestamps, plus the last failure code and up to 20 stills. The API key and the chat user's name are not stored. Stills are saved only when the path stays inside the plugin data directory. If you placed this plugin by hand, run `hermes plugins enable print-job-watch` before `hermes print-job-watch` appears.

## Skill

The plugin registers one skill, `octoprint-job`. It tells the model to look up `print_job_status`, `print_job_control`, and `print_job_watch` with tool_search, in English, before it tries web_search. The skill runs nothing, sends nothing, and approves nothing by itself. A move still returns an approval line. Session and always cover only the same action at the same origin while a person is present. cron, `hermes -q`, and webhook refuse the move, including after always.

On Hermes 0.21.6 and later, while the plugin is enabled, the skill adds one line to the skills index of every session. That includes a surface where the tools were turned off with `hermes tools`: the line stays there, and the calls are simply unavailable. On Hermes 0.21.4 and 0.21.5 the skill is not listed; it loads only when called by its full name, `print-job-watch:octoprint-job`. There is no separate user list for the skill; every session that loads the plugin can read it. `hermes plugins disable print-job-watch` removes the skill, the tools, and the slash command together. A watch already scheduled is a Hermes cron job that disabling does not remove: run `hermes print-job-watch unschedule` before you disable the plugin, or `hermes cron remove <id>` after.

## Moves

Every pause, resume, and cancel returns `{"action": "approve", "message": <one line>}` from the `pre_tool_call` hook. Hermes then runs the same human gate as a dangerous shell command (`hermes_cli/plugins.py` `_resolve_block_from_details` calls `tools/approval.py` `request_tool_approval`). The line names the OctoPrint origin and the `POST /api/job` body. `print_job_status` and `print_job_watch` return nothing from the hook. The `rule_key` is a digest of the action and the OctoPrint origin. session and always apply only to that same plan. A different action or origin asks again. Choosing always writes one `plugin_rule:` line to `command_allowlist`. Remove it by editing `command_allowlist` in the Hermes config. If the question, or its HTML-escaped form, is longer than 300 characters, the hook blocks and nothing is posted. The wait for a person is `approvals.timeout` (300 seconds unless you changed it). That is not the 300-character limit. After a yes, the handler reads the printer again and posts only when that plan still matches. If the state changed, it posts nothing and returns `isError`. When the pre_tool_call dispatcher returns BLOCKED, Hermes does not call the handler (`model_tools.py` `handle_function_call` returns at line 942 in v0.22.2 and line 945 in the newer checkout before `_execute_tool`, and `agent/tool_executor.py` `_dispatch_authorized_once` returns at line 738 in v0.22.2 and line 734 in the newer checkout before `execute`).

Hermes decides the modes. This plugin does not read them:

- yolo (`--yolo` / `/yolo`) and `approvals.mode: off` skip the prompt and treat the call as approved while a person is present (`tools/approval.py` `_run_approval_gate`, the check at line 967 in v0.22.2). This plugin does not add a second refusal on top of those two modes.
- cron, `hermes -q`, webhook, and other unattended sessions are refused in the `pre_tool_call` hook before Hermes consults a saved always line. The same refusal runs again in the handler, before any POST. Answering always does not make a later cron, `-q`, or webhook call send the move.

A `plugins.isolation` host process (`HERMES_PLUGIN_HOST_PROCESS` set to anything other than empty or `0`, including `1`) still refuses the move, because that process cannot show the conversation. Requires Hermes `>=0.21.6`, the first stable release that provides `ctx.current_cron_execution` (the watch uses it to tell a cron run from a manual one; without it the watch would say it cannot run because it looks like `plugins.isolation: host`) and that applies another plugin's `block` before `approve`.

An empty `HERMES_HOME` scan with Hermes v0.22.2 and with origin-main is `Verdict: SAFE`, 12 findings, `Decision: ALLOWED — Allowed (community source, safe verdict)`. The findings are the JPEG marker in the snapshot check, the API key names in the changelog, and test subprocesses. They stay, because the snapshot check and the tests need them.

`POST /api/job` bodies are only:

- `{"command":"pause","action":"pause"}` (never `toggle`)
- `{"command":"pause","action":"resume"}` (continues; it does not restart the file)
- `{"command":"cancel"}` (cannot be resumed)

Pause is sent only for `Printing` and `Printing from SD`. It is not sent for `Paused`. Pause is not sent while the state is `Resuming` or starts with `Starting` (including `Starting print from SD` and `Starting to send file to SD`). The print keeps going, and the reply says so. `Sending file to SD` and `Transferring file to SD` are not paused; the transfer is still going and nothing is sent. A stopped printer (`Operational`) is told that it is not printing, so pause and resume are not sent. Cancel while the job is already `Operational` says `State is already Operational. Nothing was sent.`

HTTP 204 with an empty body means OctoPrint accepted the command. It does not mean the printer has paused, resumed, or stopped. The plugin then only reads, for up to 60 seconds. It does not stop around 40 seconds. It reports a move when a later read shows `Paused`, `Printing`, or `Operational`. `Printing from SD` can be paused, and the watch treats it as printing, but a resume is reported only when the later read is exactly `Printing`. A later read of `Printing from SD` is not called a finished resume, and the message says the print may already be going. Read it again when the state is `Printing`, then ask for pause.

`Pausing` is a pause in progress, not a failure. The plugin does not send pause or resume while the state is `Pausing`. On OctoPrint 1.11.8, resume runs only when the job is already paused, so resume during `Pausing` does not continue the print. After approval, the plugin reads the state once more and does not post if it is no longer a state that should move. It then keeps reading until 60 seconds have passed. If the wait ends still in `Pausing` or `Cancelling`, the result is accepted but not moved. Read the state before sending anything else.

Cancel is reported when a later read is `Operational`. If the printer stays in `Cancelling`, the result is accepted but not moved. Do not send resume.

A changed state string is not proof the toolhead followed the command.

## Watch

`print_job_watch` takes no arguments. Passing arguments is a bad call, not a printer failure, and it is not saved.

| Event | When |
| --- | --- |
| `printer_disconnected` | Connection `Closed` or `Offline`, or job `Offline`, and the job is not an error. A leftover `Paused` string does not win over a closed connection. |
| `error` | Job `Error` or `Offline after error`, or connection `Error`. Includes the job error string. `GET /api/printer` returning 409 is not this by itself. |
| `complete` | Previous state was Printing, Printing from SD, Pausing, Paused, Resuming, or Finishing, and the job is now Operational with completion at least 100. |
| `paused` | Previous state was Printing, Printing from SD, Pausing, or Resuming, and the job is now Paused, with the connection up. The first sample does not emit this. |
| `cancelled` | Once, on entering `Cancelling`. If that tick was missed, including a watch that starts while the job is already `Cancelling`, once when the job is Operational with completion under 100 or null. The same print does not also emit `complete`. |
| `stalled` | Printing or Printing from SD, `filepos` is a number, and file position, completion, and filename stay the same for `stall_minutes` (default 10, clamped 1–240). A missing `filepos` is not a stall. |

The first sample can emit `printer_disconnected` or `error` if the printer is already in that state. It does not emit `paused` or `cancelled`. Only a cron run marks an event reported, when `print_job_watch` returns. A slash command, the CLI, or the tool called outside cron only reads, so the owner's next cron run can still report the same event. If the chat message fails after a cron run returns, the event is not sent again. `Printing from SD` is treated as printing.

If the printer cannot be checked, `notify` is true the first time, when the cause changes, and again after 24 hours. Repeats of the same cause set `notify` to false; the cron prompt tells the model to reply `[SILENT]`. The next successful check says so once. That once-per-cause behavior is only for an in-process cron turn, where the cron check can be read. Under plugins.isolation: host, this form cannot watch the printer. Set plugins.isolation to in_process. A corrupt state file is left in place. Calling `print_job_watch` with arguments is not one of those causes.

`printTimeLeft` is OctoPrint's number, including 0. Null stays null. The plugin does not invent a remaining time from `estimatedPrintTime`. On the Virtual Printer, 0 while printing was common and is not "unknown".

Temperatures are null when `GET /api/printer` returns 409 (`Printer is not operational`). That 409 is not a temperature of 0, and it is not by itself a disconnect or an error. Error states also return 409, and the flags that would say so exist only on HTTP 200.

## Schedule

```
hermes print-job-watch schedule --deliver telegram:123456 --schedule "*/5 * * * *"
```

The default schedule is `*/5 * * * *`. Each run is at least one model turn. A turn that calls a tool makes two or more model requests. If the schedule is every 5 minutes, that is 288 runs a day. There is no daily cap. When this Hermes profile is not default, printed `hermes print-job-watch` (including unschedule), `hermes cron`, and `hermes plugins remove` lines include `-p <name>`. That name is `ctx.profile_name`, read once in `register`. A missing or unusable name is printed as the default profile, with no `-p`. This plugin does not import `hermes_cli.profiles`. This plugin does not take `-p` or `--profile` as its own arguments.

The CLI takes `--deliver` and `--schedule`. Accepted targets are `origin`, `local`, `all`, `bot-chat`, `bot-chat:<profile>`, a platform Hermes cron delivers to, `platform:chat_id`, and a comma combination such as `origin,all`. `cli`, `cron`, and `api_server` are not delivery targets. `bot-chat` spends one model turn: the agent reads the report and acts on it. `local` is stored on this machine and is not sent to a chat. A `--deliver` value that looks like a schedule (`every`, `in`, `at`, a weekday, `weekdays`, `5m`, a leading digit, `*`, `@`, or an ISO timestamp) is refused, and the existing job is left as it is. A schedule faster than every 2 minutes, or one this plugin cannot measure, is refused and nothing is created.

The cron job only calls `print_job_watch`. It cannot pause, resume, or cancel.

`hermes plugins remove` does not remove the job. When this profile is not default, that printed line includes `-p <name>`. Run `hermes print-job-watch unschedule` first. Unschedule keeps the watch state and stills.

## Stills

`include_snapshot` on status fetches `OCTOPRINT_SNAPSHOT_URL` only when that URL has the same hostname as `OCTOPRINT_URL`. Another host is refused before any request. The HTTP client does not follow redirects. A response to a request that sent `X-Api-Key` is not followed, including a redirect that stays on the same origin. That call fails in place, and the key is not sent to the next URL. A still that was requested without the key may follow at most two redirects, and only while the target stays on the same hostname. A redirect to another host is not followed, and that host does not receive `X-Api-Key`. `X-Api-Key` is sent on a still only when the snapshot URL is the same origin (scheme, host, and port). A different port on the same host is fetched without the key. Only a JPEG or PNG is kept, at most `snapshot_max_bytes` (default 2_000_000, hard maximum 5_000_000). A body shorter than `Content-Length`, or a declared length over the cap, is discarded. At most 20 stills are kept, named `still-<ms>-1.jpg` or `.png`.

## Limits

JSON calls wait 10 seconds and accept at most 1_000_000 bytes. A control `POST` waits up to 60 seconds. A still waits 15 seconds. There are no retries. On a control POST, a timeout, a connection that closes after the command was sent, a body cut short after the status line, HTTP 5xx, or a non-JSON body means the command may already have reached the printer; read the state before sending it again. A read or a still that is HTTP 5xx or not JSON says nothing was saved, and does not say the command may have reached the printer. A port that is not a number from 0 to 65535 is a bad URL, and nothing is requested.

A bad API key with access control on is HTTP 403 with OctoPrint's permission error. If you POST cancel while the printer is idle, OctoPrint returns HTTP 409 and `Printer is neither printing nor paused, 'cancel' command cannot be performed`. That is a different 409 from `Printer is not operational`. This plugin does not send that POST when the job is already Operational. It says the state is already Operational and that nothing was sent.

This plugin starts no child process. `tests/` is shipped and not loaded by `register()`. Private Hermes modules that fail to import fail closed.

## What was tried

On 2026-10-07, against the official `octoprint/octoprint` image (OctoPrint 1.11.8) and its Virtual Printer, not a physical printer:

- A bad key with access control on returned HTTP 403 and the permission error text.
- `POST /api/job` for pause, resume, and cancel returned HTTP 204 with an empty body.
- A heat-and-wait file stayed in `Pausing` for the whole poll. A short `G4` did not show `Pausing` become `Paused`, and it did not show `Cancelling` last past that poll.
- Resume after a dwell that had already finished returned 204 and then `Operational` with completion 100. That is the print finishing, not a successful resume.
- Cancel during a dwell stayed in `Cancelling` with null completion for the poll (about 20 seconds) and did not settle.
- Disconnect was connection `Closed`, job `Offline`, and HTTP 409 from `/api/printer`. It was not `Paused`.
- A direct POST of idle `cancel` to OctoPrint, not through this plugin, was HTTP 409, `Printer is neither printing nor paused, 'cancel' command cannot be performed`. This plugin does not send that POST when the job is already Operational, and it does not describe that direct POST as a 409 from the plugin.
- Completion on this server is a percent (for example 46.875 or 100.0), not a fraction. A `G4` dwell reported completion 100 and `printTimeLeft` 0 while the job state was still `Printing`. `complete` is not emitted until the job is `Operational`.
- Through this plugin, with approval replaced by a stub, not by a person pressing an approval button: a denied pause left the job `Printing`; an approved pause later read `Paused`; resume then read `Operational` at completion 100 and was reported as the print finishing, not a successful resume; cancel was accepted and a later read was still `Cancelling` (completion null), so the plugin reported accepted but not moved; after disconnect the watch reported `printer_disconnected` (connection `Closed`, job `Offline`), not paused.

On 2026-10-08 the harness, on Hermes main with `plugins.isolation: host` and a real cron tick, called `print_job_watch`. The tick before the upstream changed and the tick after both returned notify true and `Under plugins.isolation: host, this form cannot watch the printer. Set plugins.isolation to in_process.` The plugin CLI was not run in that host, because Hermes does not register it there. The printer was not stepped through Printing, then Paused, then cancel.

Not tried, and not claimed here:

- A physical printer.
- A server with access control turned off. OctoPrint's documentation says that a missing or wrong key is then a full admin. That mode was not tested.
- A webcam still from this image. `ENABLE_MJPG_STREAMER` is off. Same-host and key rules are offline tests.
- Job states `Error` and `Offline after error` on this Virtual Printer.
- Job states `Printing from SD`, `Starting print from SD`, `Sending file to SD`, and `Transferring file to SD` on this Virtual Printer. Those names were classified by passing them to the offline tests.
- `Pausing` becoming `Paused`, and `Cancelling` lasting a long time, on a short `G4`.
- A slash command from a gateway room, an approval question sent to Discord, and the difference between a group approval button and a typed `/approve`. The 300-character check is a length calculation.
- `status` inside a host process.
- PrusaLink.
- Pause, resume, or cancel where a person pressed the approval button. The plugin path above stubbed approval.
