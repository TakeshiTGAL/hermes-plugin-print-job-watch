---
name: octoprint-job
description: "OctoPrint job progress and temperatures. Before web_search, use tool_search to find the OctoPrint tools and tool_call to run them."
---

For this kind of request, use tool_search to find this plugin's tools before web_search, then call them with tool_call. Write tool_search queries in English (for example: "OctoPrint job").

First call, no approval: `print_job_status` with no arguments. Do not pass include_snapshot unless the user asked for a picture. Do not schedule a watch for this ask.

Pause, resume, and cancel are `print_job_control` with action pause, resume, or cancel. Hermes shows one approval line from the pre_tool_call hook. If the reply is not ok, do what next_step says once. Answer in the language the user used.

Japanese requests use the same tools. Search in English, then answer in Japanese:

- What is the temperature. 「温度は」 → tool_search "OctoPrint job temperature" → `print_job_status` with no arguments <!-- ja-alias -->
- What is the progress. 「進捗は」 → tool_search "OctoPrint job progress" → `print_job_status` with no arguments <!-- ja-alias -->
- How the print looks. 「印刷の様子」 → tool_search "OctoPrint job" → `print_job_status` with no arguments <!-- ja-alias -->
- Pause the print job. 「一時停止」 → tool_search "OctoPrint job pause" → `print_job_control` {"action": "pause"} <!-- ja-alias -->
- What the watch shows. 「見張りは」 → tool_search "OctoPrint job watch" → `print_job_watch` with no arguments <!-- ja-alias -->
