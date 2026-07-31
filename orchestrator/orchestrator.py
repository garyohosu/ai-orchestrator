"""Entrypoint. Run directly, e.g. ``python .\\orchestrator\\orchestrator.py`` (SPEC.md 13,14章)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# SPEC.md 21章: system-wide UTF-8, independent of the launching terminal's
# codepage (Windows consoles are not UTF-8 by default).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import argparse
from dataclasses import replace

import config as config_module
from adapters import build_adapters
from dispatch import (
    AgentOutcome,
    CheckpointStore,
    DispatchCycle,
    ErrorNotifier,
    MailWatcher,
    NotificationDetail,
    OutcomeClassifier,
    OutcomeStatus,
    RetryPolicy,
    RoundTripCounter,
)
from launcher import CliLauncher, CliPathResolver
from logging_utils import JobLogger, LogEntry
from mail_adapter import MailModuleAdapter, MailPackageNotFoundError, MailReplyQuery
from paths import PathResolver, ProjectPathOutOfRangeError
from runtime import (
    ForceStopRequested,
    RecoveryActionKind,
    RunDurationGuard,
    RuntimeStateStore,
    StaleRecoveryService,
    StopController,
    TerminalStateStore,
)
from timeutil import now_iso

SYSTEM_AGENT_NAME = "orchestrator"


class StartupError(Exception):
    """A fatal error detected before the main loop can start."""


def _check_writable_dirs(*dirs: Path) -> None:
    """UI.md 2.1「権限なし状態」: unwritable logs/checkpoints/runtime aborts startup."""
    for directory in dirs:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / ".write_check"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as err:
            raise StartupError(f"{directory} へ書き込めません: {err}") from err


class OrchestratorContext:
    """Wires every component once at startup; reused by watch/once modes."""

    def __init__(self, script_path: Path) -> None:
        self.path_resolver = PathResolver.from_script(script_path)

        config_path = self.path_resolver.orchestrator_dir / "config.json"
        self.config = config_module.load(config_path)

        try:
            self.project_path = self.path_resolver.resolve_project_path(self.config.project_path)
        except ProjectPathOutOfRangeError as err:
            raise StartupError(str(err)) from err

        self.logs_dir = self.path_resolver.orchestrator_dir / self.config.logs_dir
        self.checkpoints_dir = self.path_resolver.orchestrator_dir / self.config.checkpoints_dir
        self.runtime_dir = self.path_resolver.orchestrator_dir / self.config.runtime_dir
        _check_writable_dirs(self.logs_dir, self.checkpoints_dir, self.runtime_dir)

        try:
            self.mail_adapter = MailModuleAdapter(self.path_resolver.mail_dir)
        except MailPackageNotFoundError as err:
            raise StartupError(str(err)) from err

        self.system_uid = self.mail_adapter.register_user(SYSTEM_AGENT_NAME)

        runtime_agents = []
        for agent in self.config.agents:
            if agent.cli_type == "director" and agent.uid == "AUTO":
                agent = replace(agent, uid=self.mail_adapter.register_user(agent.name))
            runtime_agents.append(agent)
        self.config = replace(self.config, agents=runtime_agents)

        for agent in self.config.agents:
            try:
                self.mail_adapter.check_mail(agent.uid)
            except self.mail_adapter.errors.UserNotFoundError as err:
                raise StartupError(
                    f"config.jsonのagents設定が未登録のUIDを参照しています: "
                    f"{agent.name} ({agent.uid}): {err}"
                ) from err

        self.logger = JobLogger(self.logs_dir)
        self.checkpoint_store = CheckpointStore(self.checkpoints_dir)
        self.runtime_store = RuntimeStateStore(self.runtime_dir)
        self.terminal_store = TerminalStateStore(self.runtime_dir)
        self.reply_query = MailReplyQuery(self.mail_adapter)
        self.notifier = ErrorNotifier(
            self.mail_adapter, self.system_uid, self.config.notification_tail_bytes
        )
        self.round_trips = RoundTripCounter(self.config.max_round_trips, runtime_dir=self.runtime_dir)

        adapters = build_adapters()
        cli_resolver = CliPathResolver(adapters)
        launcher = CliLauncher(
            cli_resolver,
            adapters,
            logs_dir=self.logs_dir,
            output_max_bytes=self.config.cli_output_max_bytes,
            output_ring_bytes=self.config.cli_output_ring_bytes,
        )
        watcher = MailWatcher(self.mail_adapter)

        self.dispatch_cycle = DispatchCycle(
            watcher=watcher,
            launcher=launcher,
            mail_adapter=self.mail_adapter,
            reply_query=self.reply_query,
            classifier=OutcomeClassifier(),
            retry_policy=RetryPolicy(self.config.max_retries),
            notifier=self.notifier,
            logger=self.logger,
            checkpoint_store=self.checkpoint_store,
            round_trips=self.round_trips,
            runtime_store=self.runtime_store,
            terminal_store=self.terminal_store,
            project_path=self.project_path,
            cli_timeout_sec=self.config.cli_timeout_sec,
            reply_check_timeout_sec=self.config.reply_check_timeout_sec,
            agents=self.config.agents,
            system_sender_uid=self.system_uid,
            max_handoffs=self.config.max_handoffs,
        )

        self.run_duration_guard = RunDurationGuard(self.config.max_run_duration_sec)
        self.stop_controller = StopController()


def _run_stale_recovery(ctx: OrchestratorContext) -> list:
    stale_service = StaleRecoveryService(ctx.runtime_store, ctx.mail_adapter, ctx.reply_query)
    actions = stale_service.recover_on_startup()
    for action in actions:
        state = action.state
        if action.kind == RecoveryActionKind.REQUEUE:
            # action.origin_mail is None only if find_mails could not even
            # locate the origin mail by ID/recipient -- normally
            # unreachable (mail is never deleted), but logged distinctly
            # rather than silently identical to the ordinary "still
            # unread" requeue, in case it ever happens.
            unusual = action.origin_mail is None
            ctx.logger.log_outcome(
                LogEntry(
                    job_id=state.job_id,
                    mail_id=state.origin_mail_id,
                    agent_name=state.agent_name,
                    command_summary=None,
                    started_at=None,
                    finished_at=now_iso(),
                    exit_code=None,
                    result="STALE_REQUEUE_ORIGIN_NOT_FOUND" if unusual else "STALE_REQUEUE",
                )
            )
        elif action.kind == RecoveryActionKind.MARK_COMPLETED:
            ctx.terminal_store.mark(state.origin_mail_id, OutcomeStatus.SUCCESS.value)
            ctx.logger.log_outcome(
                LogEntry(
                    job_id=state.job_id,
                    mail_id=state.origin_mail_id,
                    agent_name=state.agent_name,
                    command_summary=None,
                    started_at=None,
                    finished_at=now_iso(),
                    exit_code=None,
                    result="STALE_MARK_COMPLETED",
                )
            )
        elif action.kind == RecoveryActionKind.NOTIFY_ORIGIN_SENDER:
            detail = NotificationDetail(
                target_agent_name=state.agent_name,
                target_agent_uid=state.agent_uid,
                failed_stage="STALE復旧",
                reason="オーケストレーター再起動時に対応する実行中プロセスが見つかりませんでした",
                exit_code=None,
                duration_sec=None,
                retry_count=state.retry_count,
                last_attempt_at=now_iso(),
                origin_mail_status="既読・返信なし",
                recommended_action=(
                    f"{state.agent_name}の状態を確認し、必要であれば同じ依頼IDで再送してください。"
                ),
            )
            notified = ctx.notifier.notify(
                state.job_id,
                state.origin_mail_id,
                action.origin_mail["sender_uid"],
                OutcomeStatus.FAILED,
                detail,
            )
            final_status = OutcomeStatus.FAILED if notified else OutcomeStatus.HUMAN_REQUIRED
            ctx.terminal_store.mark(state.origin_mail_id, final_status.value)
            ctx.logger.log_outcome(
                LogEntry(
                    job_id=state.job_id,
                    mail_id=state.origin_mail_id,
                    agent_name=state.agent_name,
                    command_summary=None,
                    started_at=None,
                    finished_at=now_iso(),
                    exit_code=None,
                    result=f"STALE_{final_status.value}",
                    next_recipient=action.origin_mail["sender_uid"] if notified else None,
                )
            )
    return actions


def _print_stale_summary(actions: list) -> None:
    if not actions:
        return
    print(f"STALE復旧: {len(actions)}件")
    for action in actions:
        print(f"  - {action.state.job_id}: {action.kind.value}")


def _print_cold_start_notice_if_applicable(ctx: OrchestratorContext) -> None:
    """UI.md 2.1「未着手状態（コールドスタート）」の案内表示。"""
    try:
        if any(ctx.mail_adapter.check_mail(agent.uid) > 0 for agent in ctx.config.agents):
            return
        history_exists = False
        for agent in ctx.config.agents:
            if ctx.mail_adapter.find_mails(sender_uid=agent.uid, limit=1):
                history_exists = True
                break
            if ctx.mail_adapter.find_mails(recipient_uid=agent.uid, limit=1):
                history_exists = True
                break
        if not history_exists:
            print(
                "指揮AIを手動起動して最初の作業依頼を送信してください。"
                "(SPEC.md 9.1, 29章)"
            )
    except Exception as err:
        ctx.logger.log_outcome(
            LogEntry(
                job_id=None,
                mail_id=None,
                agent_name=None,
                command_summary=None,
                started_at=None,
                finished_at=now_iso(),
                exit_code=None,
                result="COLD_START_CHECK_FAILED",
                error=str(err),
            )
        )


def _print_outcome(outcome: AgentOutcome) -> None:
    print(f"{outcome.agent.name}: {outcome.status.value} (job={outcome.job_id})")


def run_once_mode(ctx: OrchestratorContext) -> int:
    actions = _run_stale_recovery(ctx)
    _print_stale_summary(actions)
    _print_cold_start_notice_if_applicable(ctx)

    outcomes = ctx.dispatch_cycle.run_one_pass(ctx.config.agents)
    if not outcomes:
        print("対象なし（NO_WORK）")
        ctx.logger.log_outcome(
            LogEntry(
                job_id=None,
                mail_id=None,
                agent_name=None,
                command_summary=None,
                started_at=None,
                finished_at=now_iso(),
                exit_code=None,
                result="NO_WORK",
            )
        )
    else:
        for outcome in outcomes:
            _print_outcome(outcome)
    success = sum(1 for o in outcomes if o.status == OutcomeStatus.SUCCESS)
    failed = len(outcomes) - success
    print(f"一巡完了: 処理={len(outcomes)}件 (成功={success}, 失敗={failed})")
    return 0


def run_watch_mode(ctx: OrchestratorContext) -> int:
    ctx.stop_controller.install()
    actions = _run_stale_recovery(ctx)
    _print_stale_summary(actions)
    _print_cold_start_notice_if_applicable(ctx)
    print(f"常時監視モードを開始します。project_root={ctx.path_resolver.project_root}")

    def should_stop_launching() -> bool:
        return ctx.stop_controller.stop_requested or ctx.runtime_store.read_stop_request()

    while True:
        if should_stop_launching():
            print("停止要求を受け付けました。実行中のAI終了を待機します。")
            break
        if ctx.run_duration_guard.should_stop():
            print("最大連続実行時間に達したため安全停止します。")
            break
        try:
            outcomes = ctx.dispatch_cycle.run_one_pass(
                ctx.config.agents, should_stop_launching=should_stop_launching
            )
        except ForceStopRequested:
            print("強制停止します。")
            ctx.logger.log_outcome(
                LogEntry(
                    job_id=None,
                    mail_id=None,
                    agent_name=None,
                    command_summary=None,
                    started_at=None,
                    finished_at=now_iso(),
                    exit_code=None,
                    result="FORCE_STOPPED",
                    error="二重のCtrl+Cにより強制停止しました",
                )
            )
            ctx.runtime_store.clear_stop_request()
            return 1
        for outcome in outcomes:
            _print_outcome(outcome)
        time.sleep(ctx.config.mail_check_interval_sec)

    ctx.runtime_store.clear_stop_request()
    print("オーケストレーターを終了します。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orchestrator.py")
    parser.add_argument("--once", action="store_true", help="全AIの未読を一度確認して終了する")
    args = parser.parse_args(argv)

    try:
        ctx = OrchestratorContext(Path(__file__))
    except StartupError as err:
        print(f"[エラー] 起動時チェックに失敗しました: {err}", file=sys.stderr)
        return 1

    if args.once:
        return run_once_mode(ctx)
    return run_watch_mode(ctx)


if __name__ == "__main__":
    raise SystemExit(main())
