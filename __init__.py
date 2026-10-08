"""Print job watch for Hermes, built on the OctoPrint REST API."""

import asyncio
import concurrent.futures


def register(ctx) -> None:
    if __package__:
        from .service import (
            DEFAULT_SCHEDULE,
            TOOLSET,
            control,
            deps_from_config,
            hermes_line,
            schedule,
            status,
            unschedule,
            watch,
        )
    else:
        from service import (
            DEFAULT_SCHEDULE,
            TOOLSET,
            control,
            deps_from_config,
            hermes_line,
            schedule,
            status,
            unschedule,
            watch,
        )

    def _deps():
        import os
        return deps_from_config(
            ctx.get_config,
            url=os.environ.get("OCTOPRINT_URL", ""),
            api_key=os.environ.get("OCTOPRINT_API_KEY", ""),
            snapshot_url=os.environ.get("OCTOPRINT_SNAPSHOT_URL", ""),
        )

    def _status(args, **_kwargs):
        return status(_deps(), args or {})

    def _control(args, **_kwargs):
        return control(_deps(), args or {})

    def _watch(args, **_kwargs):
        return watch(_deps(), args or {})

    ctx.register_tool(
        name="print_job_status",
        toolset=TOOLSET,
        schema={
            "name": "print_job_status",
            "description": (
                "Read one OctoPrint printer through the configured URL. "
                "Returns job state, connection state, completion, temperatures, and printTimeLeft. "
                "printTimeLeft is OctoPrint's estimate and may be 0. A disconnected printer is not described as paused. "
                "include_snapshot saves one JPEG or PNG still only after Hermes's write guard allows it. "
                "Does not move the printer and cannot change the URL."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "include_snapshot": {"type": "boolean", "description": "Save one webcam still. Default false."},
                },
            },
        },
        handler=_status,
        emoji="🖨️",
    )
    ctx.register_tool(
        name="print_job_control",
        toolset=TOOLSET,
        schema={
            "name": "print_job_control",
            "description": (
                "Pause, resume, or cancel the configured printer. "
                "Asks Hermes for approval on every call; an earlier session or always answer is not reused. "
                "Answering always adds one unused allowlist line; choose once. "
                "Does nothing if this is cron, yolo, approvals off, single-query, an unattended session, "
                "or a plugins.isolation host process, or if that check cannot be loaded. "
                "Pause is sent only for Printing and Printing from SD, not for Paused. "
                "Pause is not sent during Resuming or a state that starts with Starting, "
                "including Starting print from SD and Starting to send file to SD; the print is still going. "
                "Sending file to SD and Transferring file to SD are not paused. "
                "Pause sends action pause, never toggle. Resume continues; it does not restart. "
                "HTTP 204 means accepted, not finished. Pausing is not a failure; do not send resume during Pausing. "
                "Reports a move only when a later read shows Paused, Printing, or Operational."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["pause", "resume", "cancel"]},
                },
                "required": ["action"],
            },
        },
        handler=_control,
        emoji="⏸️",
    )
    ctx.register_tool(
        name="print_job_watch",
        toolset=TOOLSET,
        schema={
            "name": "print_job_watch",
            "description": (
                "Compare the printer with the previous sample. Reports printer_disconnected, error, "
                "complete (after Printing, Printing from SD, Pausing, Paused, Resuming, or Finishing), "
                "paused (after Printing, Printing from SD, Pausing, or Resuming, with the connection up), "
                "cancelled, and stalled (Printing or Printing from SD, with a numeric filepos). "
                "printer_disconnected is Offline or Closed, not paused. "
                "error is Error or Offline after error, even when /api/printer returns 409. "
                "The first sample does not emit paused or cancelled. "
                "If the printer cannot be checked in process, notify is true on the first failure, when the cause changes, "
                "and once a day; other repeats are silent, and the next working check says so once. "
                "A later chat failure does not send a cron event again. "
                "Under plugins.isolation: host, this form cannot watch the printer. Set plugins.isolation to in_process. "
                "Arguments are refused and not recorded as a failure. Does not move the printer. Takes no arguments."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        handler=_watch,
        emoji="👀",
    )

    def _slash_work(raw_args: str) -> str:
        parts = (raw_args or "").split()
        cmd = parts[0] if parts else "status"
        if cmd in {"pause", "resume", "cancel"}:
            return (
                "pause, resume, and cancel are not on the slash command. "
                "Ask in the chat so the tool can request approval. Nothing was sent."
            )
        if cmd in {"schedule", "unschedule"}:
            return (
                "schedule and unschedule are not on the slash command. "
                f"Use `{hermes_line('print-job-watch schedule')}` or "
                f"`{hermes_line('print-job-watch unschedule')}` on this machine. "
                "A chat user cannot change or remove the cron job from here."
            )
        if cmd not in {"status", "watch"}:
            return (
                "Usage: /print-job-watch status | watch. "
                "pause, resume, and cancel are tools, and each one asks for approval. "
                "schedule and unschedule are only on the CLI."
            )
        deps = _deps()
        if cmd == "watch":
            return watch(deps, {}, record=False)
        return status(deps, {})

    slash_pool = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="print-job-watch-slash")

    async def _slash(raw_args: str) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(slash_pool, _slash_work, raw_args)

    ctx.register_command(
        "print-job-watch",
        handler=_slash,
        description="Read the configured OctoPrint printer. It does not pause, resume, or cancel.",
    )

    def _setup(parser) -> None:
        subs = parser.add_subparsers(dest="print_job_command")
        subs.add_parser("status", help="Read printer state. Does not move it.")
        subs.add_parser("watch", help="Compare with the previous sample. Does not move the printer.")
        scheduled = subs.add_parser(
            "schedule",
            help="Create a Hermes cron job that only calls print_job_watch. Each run is at least one model turn.",
        )
        scheduled.add_argument(
            "--deliver",
            default="",
            help=(
                "origin, local, all, bot-chat, bot-chat:<profile>, a platform, or platform:chat_id. "
                "cli, cron, and api_server are not delivery targets. Required."
            ),
        )
        scheduled.add_argument("--schedule", default=DEFAULT_SCHEDULE, help="Cron expression. No faster than every 2 minutes.")
        subs.add_parser("unschedule", help="Remove the cron job. Keeps watch state and snapshots.")

    def _cli(args) -> None:
        cmd = getattr(args, "print_job_command", None) or "status"
        deps = _deps()
        if cmd == "watch":
            print(watch(deps, {}, record=False))
        elif cmd == "schedule":
            print(schedule(deps, args.schedule, args.deliver))
        elif cmd == "unschedule":
            print(unschedule(deps))
        else:
            print(status(deps, {}))

    ctx.register_cli_command(
        name="print-job-watch",
        help="Watch an OctoPrint printer.",
        setup_fn=_setup,
        handler_fn=_cli,
        description="Read OctoPrint and schedule a watch. Pause, resume, and cancel stay on the tool, which asks for approval.",
    )


__all__ = ["register"]
