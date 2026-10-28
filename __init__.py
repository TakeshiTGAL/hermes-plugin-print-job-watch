"""Print job watch for Hermes, built on the OctoPrint REST API."""

import asyncio
import concurrent.futures
import contextvars


def register(ctx) -> None:
    if __package__:
        from .gates import on_pre_tool_call, set_origin_getter
        from .client import parse_origin
        from .service import (
            DEFAULT_SCHEDULE,
            TOOLSET,
            bind_cron_reader,
            control,
            deps_from_config,
            hermes_line,
            remember_profile_name,
            schedule,
            status,
            unschedule,
            watch,
        )
    else:
        from gates import on_pre_tool_call, set_origin_getter
        from client import parse_origin
        from service import (
            DEFAULT_SCHEDULE,
            TOOLSET,
            bind_cron_reader,
            control,
            deps_from_config,
            hermes_line,
            remember_profile_name,
            schedule,
            status,
            unschedule,
            watch,
        )

    def _deps():
        from agent.secret_scope import get_secret
        return deps_from_config(
            ctx.get_config,
            url=get_secret("OCTOPRINT_URL") or "",
            api_key=get_secret("OCTOPRINT_API_KEY") or "",
            snapshot_url=get_secret("OCTOPRINT_SNAPSHOT_URL") or "",
        )

    try:
        reported = ctx.profile_name
    except Exception:
        reported = None
    remember_profile_name(reported)

    def _status(args, **_kwargs):
        return status(_deps(), args or {})

    def _control(args, **kwargs):
        return control(_deps(), args or {}, **kwargs)

    def _watch(args, **_kwargs):
        return watch(_deps(), args or {})

    ctx.register_tool(
        name="print_job_status",
        toolset=TOOLSET,
        schema={
            "name": "print_job_status",
            "description": (
                "OctoPrint printer: read job progress and temperatures. "
                "Returns job state, connection state, completion, temperatures, and printTimeLeft. "
                "printTimeLeft says how long until the print finishes, and it may be 0. A disconnected printer is not described as paused. "
                "include_snapshot saves one JPEG or PNG camera view of the printer, and only when the path stays inside the plugin data directory. "
                "Does not move the printer and cannot change the URL. "
                "It reads a 3D printer, including the hotend temperature. "
                "The current job includes print time remaining, and include_snapshot can save a webcam snapshot."
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
                "OctoPrint printer: pause, resume, or cancel the job. "
                "Cancel stops the print. "
                "Hermes shows one pre_tool_call line and asks before the post. "
                "always on pause or resume applies only to that action at that origin while a person is present. "
                "Hermes yolo and approvals off skip that prompt while a person is present. "
                "Cron, hermes -q (single-query), webhook, and unattended sessions refuse the move, including after always. "
                "A plugins.isolation host process refuses the move. "
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
                "OctoPrint printer: report a finished or failed job. Reports printer_disconnected, error, "
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
        return await loop.run_in_executor(slash_pool, contextvars.copy_context().run, _slash_work, raw_args)

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
            help="Create a Hermes cron job named print-job-watch. Each run costs 0 tokens and holds no tools. The job runs a script and does not call a model.",
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
        subs.add_parser("unschedule", help="Remove the cron job and the watch script. Keeps watch state and snapshots.")

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
    try:
        from pathlib import Path
        ctx.register_skill(
            "octoprint-job",
            Path(__file__).resolve().parent / "skills" / "octoprint-job" / "SKILL.md",
            description=(
                "OctoPrint job progress and temperatures. "
                "Before web_search, use tool_search to find the OctoPrint tools and tool_call to run them."
            ),
        )
    except Exception:
        import logging
        logging.getLogger(__name__).warning("print-job-watch skill was not registered", exc_info=True)

    def _origin() -> str:
        from agent.secret_scope import get_secret
        raw = get_secret("OCTOPRINT_URL") or ""
        try:
            return parse_origin(raw).value
        except Exception:
            return raw

    set_origin_getter(_origin)
    if hasattr(ctx, "current_cron_execution"):
        bind_cron_reader(ctx.current_cron_execution)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)


__all__ = ["register"]
