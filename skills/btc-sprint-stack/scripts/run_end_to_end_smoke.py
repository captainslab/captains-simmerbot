from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from execution.reconciliation import ReconciliationResult, broker_order_from_dict, reconcile_trade
from reporting.performance_summary import PerformanceSummaryConfig, build_performance_summary
from reporting.session_report import build_session_report, load_session_events
from runtime.mode_launcher import launch_active_mode
from runtime.startup_check import run_startup_check
from scripts.prove_live_ready import run_live_ready_probe


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit(events: list['SmokeEvent'], event_type: str, **details: Any) -> None:
    events.append(
        SmokeEvent(
            event_type=event_type,
            timestamp=_utc_now(),
            details=details,
        )
    )


@dataclass(frozen=True)
class SmokeEvent:
    event_type: str
    timestamp: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SmokeResult:
    verdict: str
    reasons: tuple[str, ...]
    events: tuple[SmokeEvent, ...]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload['events'] = [asdict(event) for event in self.events]
        return payload


def run_end_to_end_smoke(
    profile_path: Path,
    *,
    startup_checker: Callable[..., Any] = run_startup_check,
    mode_launcher: Callable[..., Any] = launch_active_mode,
    probe_runner: Callable[..., Any] = run_live_ready_probe,
    session_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> SmokeResult:
    events: list[SmokeEvent] = []
    try:
        profile = json.loads(profile_path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return _finalize(events, verdict='smoke_fail', reasons=('missing_smoke_profile',))
    except json.JSONDecodeError:
        return _finalize(events, verdict='smoke_fail', reasons=('invalid_smoke_profile_json',))

    required = ('deployment_mode', 'active_profile', 'operator_start_request', 'startup_prerequisites', 'reconciliation_payload')
    missing = tuple(f'missing_smoke_field:{field}' for field in required if field not in profile)
    if missing:
        return _finalize(events, verdict='smoke_fail', reasons=missing)

    deployment_mode = profile['deployment_mode']
    active_profile = profile.get('active_profile')
    operator_start_request = profile['operator_start_request']
    startup_prerequisites = profile['startup_prerequisites']
    reconciliation_payload = profile['reconciliation_payload']
    allow_live_capable_smoke = bool(profile.get('allow_live_capable_smoke', False))
    performance_config = profile.get('performance_config') or {
        'min_sessions_for_review': 1,
        'min_trades_for_review': 0,
        'min_notional_for_review': 0.0,
        'repeated_mismatch_threshold': 2,
    }

    _emit(events, 'startup_check_started', mode=deployment_mode.get('mode'))
    startup_result = startup_checker(
        deployment_mode=deployment_mode,
        active_profile=active_profile,
        prerequisites=startup_prerequisites,
    )
    startup_payload = startup_result.as_dict() if hasattr(startup_result, 'as_dict') else dict(startup_result)
    _emit(
        events,
        'startup_check_completed',
        status=startup_payload['status'],
        reasons=list(startup_payload['reasons']),
    )
    if startup_payload['status'] == 'startup_blocked':
        return _finalize(events, verdict='smoke_fail', reasons=tuple(f'startup:{reason}' for reason in startup_payload['reasons']))

    mode = str(deployment_mode.get('mode'))
    if mode in {'capped_live', 'promoted_live', 'rolled_back'} and not allow_live_capable_smoke:
        probe_config_path = Path(startup_prerequisites['readiness_probe_config_path'])
        probe_result = probe_runner(probe_config_path)
        probe_payload = probe_result.as_dict() if hasattr(probe_result, 'as_dict') else {
            'status': getattr(probe_result, 'status', 'blocked'),
            'reasons': list(getattr(probe_result, 'reasons', ('probe_unavailable',))),
        }
        _emit(
            events,
            'readiness_probe_completed',
            status=probe_payload['status'],
            reasons=list(probe_payload['reasons']),
        )
        return _finalize(
            events,
            verdict='smoke_caution',
            reasons=('live_mode_stopped_at_probe',) + tuple(f'probe:{reason}' for reason in probe_payload['reasons']),
        )

    session_event_log_path = Path(startup_prerequisites['session_event_log_path'])
    _emit(events, 'runtime_launch_started', mode=mode)
    launch_result = mode_launcher(
        deployment_mode=deployment_mode,
        active_profile=active_profile,
        operator_start_request=operator_start_request,
        session_runner=session_runner,
        session_event_log_path=session_event_log_path,
    )
    launch_payload = launch_result.as_dict() if hasattr(launch_result, 'as_dict') else dict(launch_result)
    launch_status = launch_payload.get('status')
    _emit(
        events,
        'runtime_launch_completed',
        status=launch_status,
        route=launch_payload.get('route'),
        reasons=list(launch_payload.get('reasons', [])),
    )
    if launch_status == 'blocked':
        return _finalize(events, verdict='smoke_fail', reasons=tuple(f'launch:{reason}' for reason in launch_payload.get('reasons', ())))
    if launch_status == 'disabled':
        return _finalize(events, verdict='smoke_pass', reasons=('disabled_mode_verified',))

    if not session_event_log_path.exists():
        return _finalize(events, verdict='smoke_fail', reasons=('missing_session_event_log',))

    reconciliation_result = _run_reconciliation_payload(reconciliation_payload)
    _emit(
        events,
        'reconciliation_completed',
        status=reconciliation_result.status,
        reasons=list(reconciliation_result.reasons),
    )

    session_report = build_session_report(load_session_events(session_event_log_path))
    session_report_output_path = Path(startup_prerequisites['session_report_output_path'])
    session_report_output_path.parent.mkdir(parents=True, exist_ok=True)
    session_report_output_path.write_text(json.dumps(session_report.as_dict(), sort_keys=True), encoding='utf-8')
    _emit(
        events,
        'session_report_completed',
        verdict=session_report.final_session_verdict,
        reasons=list(session_report.verdict_reasons),
    )

    performance_summary = build_performance_summary(
        [session_report],
        config=PerformanceSummaryConfig(
            min_sessions_for_review=int(performance_config.get('min_sessions_for_review', 1)),
            min_trades_for_review=int(performance_config.get('min_trades_for_review', 0)),
            min_notional_for_review=float(performance_config.get('min_notional_for_review', 0.0)),
            repeated_mismatch_threshold=int(performance_config.get('repeated_mismatch_threshold', 2)),
        ),
    )
    _emit(
        events,
        'performance_summary_completed',
        verdict=performance_summary.promotion_verdict,
        reasons=list(performance_summary.promotion_reason_codes),
    )

    fail_reasons: list[str] = []
    caution_reasons: list[str] = []
    if reconciliation_result.status == 'unresolved':
        caution_reasons.append('reconciliation:unresolved')
    elif reconciliation_result.status == 'mismatch':
        caution_reasons.append('reconciliation:mismatch')

    if session_report.final_session_verdict == 'blocked':
        fail_reasons.extend(f'session_report:{reason}' for reason in session_report.verdict_reasons or ('blocked',))
    elif session_report.final_session_verdict == 'caution':
        caution_reasons.extend(f'session_report:{reason}' for reason in session_report.verdict_reasons)

    if performance_summary.promotion_verdict == 'blocked':
        fail_reasons.extend(f'performance:{reason}' for reason in performance_summary.promotion_reason_codes or ('blocked',))
    elif performance_summary.promotion_verdict == 'stay_capped':
        caution_reasons.extend(f'performance:{reason}' for reason in performance_summary.promotion_reason_codes if reason != 'sample_thresholds_met')

    if startup_payload['status'] == 'startup_dry_run_only':
        caution_reasons.extend(f'startup:{reason}' for reason in startup_payload['reasons'])

    if fail_reasons:
        return _finalize(events, verdict='smoke_fail', reasons=tuple(fail_reasons))
    if caution_reasons:
        return _finalize(events, verdict='smoke_caution', reasons=tuple(caution_reasons))
    return _finalize(events, verdict='smoke_pass', reasons=('operator_chain_validated',))


def _run_reconciliation_payload(payload: Mapping[str, Any]) -> ReconciliationResult:
    from execution.reconciliation import balance_snapshot_from_dict, order_intent_from_dict

    intent = order_intent_from_dict(dict(payload['order_intent']))
    broker_order = None
    if payload.get('broker_order') is not None:
        broker_order = broker_order_from_dict(dict(payload['broker_order']))
    balance_before = balance_snapshot_from_dict(dict(payload['balance_before']))
    balance_after = balance_snapshot_from_dict(dict(payload['balance_after']))
    return reconcile_trade(
        intent=intent,
        broker_order=broker_order,
        balance_before=balance_before,
        balance_after=balance_after,
    )


def _write_event_log(result: SmokeResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        for event in result.events:
            handle.write(json.dumps(asdict(event), sort_keys=True) + '\n')


def _finalize(events: list[SmokeEvent], *, verdict: str, reasons: tuple[str, ...]) -> SmokeResult:
    _emit(events, 'smoke_verdict_emitted', verdict=verdict, reasons=list(reasons))
    return SmokeResult(
        verdict=verdict,
        reasons=reasons,
        events=tuple(events),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Run the normalized end-to-end BTC operator smoke path')
    parser.add_argument('--config', required=True, type=Path, help='Path to the normalized smoke profile JSON')
    parser.add_argument('--event-log', type=Path, help='Optional append-only JSONL smoke event log path')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_end_to_end_smoke(args.config)
    if args.event_log is not None:
        _write_event_log(result, args.event_log)
    print(json.dumps(result.as_dict(), sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
