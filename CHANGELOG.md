# Changelog

## 0.1.7

The print-job-watch cron prompt tells the model that its first action is the tool_search tool, not text, with the query OctoPrint printer left unchanged, then to call tool_call so that its calls entry names print_job_watch with an empty arguments object, and not to reply until that tool has returned. If that tool is not in the tool list, the prompt tells the model to find it with tool_search and call it through tool_call. It does not say "Do not search". Web search is named separately: "Do not use web search." Running schedule again creates a new job with that prompt and removes the previous print-job-watch job. The previous job's prompt field is not edited in place.

## 0.1.6

On a cron run whose public job name is `print-job-watch`, the `pre_tool_call` hook allows `print_job_watch` and the three bridge tools `tool_search`, `tool_describe`, and `tool_call`, and blocks every other tool before it runs. A `tool_call` that names another deferred tool reaches that hook under the inner name, so the other tool stays blocked. Pause, resume, and cancel stay refused. A cron job with another name, and a normal chat, are unchanged.

## 0.1.5

On a cron run whose public job name is `print-job-watch`, the `pre_tool_call` hook blocks every tool except `print_job_watch` before it runs. `hermes cron create` still cannot set a toolset, so the model can see the profile's other tool names. A cron job with another name, and a normal chat, are unchanged. Cancel uses a new rule key on every call. Answering always on cancel does not cover the next cancel. Pause and resume keep one rule key for that action at that origin while a person is present.

## 0.1.4

Pause, resume, and cancel are refused in cron, `hermes -q`, webhook, and other unattended sessions, including after always. The `pre_tool_call` hook blocks before Hermes reads a saved allowlist line, and the handler blocks again before any POST. yolo and `approvals.mode: off` are unchanged: while a person is present, Hermes skips the prompt. Scheduling uses the public `hermes cron create`, `list`, and `remove` commands. `hermes cron create` cannot set a toolset, so the job uses the profile's cron toolsets. The watch that cannot run under `plugins.isolation: host` now returns `error_type` `cannot_watch`, the same value as `error`.

## 0.1.3

Pause, resume, and cancel no longer call the private approval modules. The `pre_tool_call` hook returns one approve line and Hermes asks. The manifest lists that hook. A read or a watch still returns nothing from the hook.

yolo and `approvals.mode: off` skip the prompt while a person is present. In 0.1.0 those modes refused the move. Answering always writes one permanent `plugin_rule:` line for that action at that origin. In 0.1.0 that answer added a line the next move did not use. While a person is present, the same action at the same origin now runs without asking until that line is removed. cron, `hermes -q`, and webhook do not use the line.

A response to a request that sent the API key is not followed on a redirect, including one that stays on the same origin. The key is not sent to the next URL.

## 0.1.2

The README opens with the five steps of the first request: install, `OCTOPRINT_URL`, `OCTOPRINT_API_KEY`, OctoPrint already running, and `print_job_status` with no arguments. That read does not ask for approval. Pause, resume, and cancel stay on `print_job_control` and each one still asks once. The skill turns a Japanese request into an English tool_search query and names `action pause` for a pause.

Temperature uses print_job_status with no arguments. 日本語: 温度は引数なしの print_job_status。一時停止は action pause で、承認は1回。

## 0.1.1

Adds the `octoprint-job` skill. It tells the model to find the OctoPrint tools with tool_search before web_search. On Hermes 0.21.6 and later, while the plugin is enabled, it adds one line to the skills index of every session. It does not set requires_toolsets, so it stays listed when Tool Search is left at its default, which hides this plugin's tools, and also on a surface where the tools were turned off with hermes tools. On Hermes 0.21.4 and 0.21.5 it is not listed and loads only by its full name.

action in any letter case, and the words true and false, are accepted. 10.0, fullwidth digits, extra arguments, and a missing action are refused with a fix and an example, and the caller's value is not copied into the message.

`OCTOPRINT_URL`, `OCTOPRINT_API_KEY`, and `OCTOPRINT_SNAPSHOT_URL` were read from `os.environ` on each call. One Hermes process can serve more than one profile, so that read can use another profile's printer URL and API key. Each call now reads them with `agent.secret_scope.get_secret`. An HTTP proxy set in the environment is not used for OctoPrint requests. With more than one profile, the slash commands use that profile's secret scope.

Each tool description now opens with OctoPrint printer and what the call does. Under Tool Search's default, the first sentence is the part of the description the model sees before it searches.

The README describes the per-profile reading of OCTOPRINT_URL, OCTOPRINT_API_KEY, and OCTOPRINT_SNAPSHOT_URL, the skill, where it is listed, and how to remove it.
