"""Validated exception lifecycle transitions with immutable audit rows."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from control_tower.db import utc_now
from control_tower.enums import ExceptionStatus
from control_tower.exceptions.contracts import normalize_as_of
from control_tower.models import ExceptionHistory, ExceptionRecord

LEGAL_TRANSITIONS: dict[ExceptionStatus, frozenset[ExceptionStatus]] = {
    ExceptionStatus.OPEN: frozenset({ExceptionStatus.ACKNOWLEDGED, ExceptionStatus.DISMISSED}),
    ExceptionStatus.ACKNOWLEDGED: frozenset(
        {ExceptionStatus.IN_PROGRESS, ExceptionStatus.DISMISSED}
    ),
    ExceptionStatus.IN_PROGRESS: frozenset({ExceptionStatus.RESOLVED, ExceptionStatus.DISMISSED}),
    ExceptionStatus.RESOLVED: frozenset(),
    ExceptionStatus.DISMISSED: frozenset(),
}


class InvalidTransition(ValueError):
    """Raised when a requested lifecycle transition is not legal."""


def transition_exception(
    session: Session,
    exception_id: int,
    to_status: ExceptionStatus,
    *,
    actor: str,
    reason: str | None = None,
    changed_at: datetime | None = None,
) -> ExceptionRecord:
    """Lock, validate, update, and audit exactly one lifecycle transition."""

    actor_value = actor.strip() if isinstance(actor, str) else ""
    if not actor_value:
        raise ValueError("actor must be nonblank")
    reason_value = reason.strip() if isinstance(reason, str) else None
    if to_status in (ExceptionStatus.RESOLVED, ExceptionStatus.DISMISSED) and not reason_value:
        raise ValueError("reason is required when resolving or dismissing an exception")
    instant = normalize_as_of(changed_at) if changed_at is not None else utc_now()
    record = session.get(ExceptionRecord, exception_id, with_for_update=True)
    if record is None:
        raise LookupError(f"exception {exception_id} does not exist")
    if to_status not in LEGAL_TRANSITIONS[record.status]:
        raise InvalidTransition(f"{record.status} cannot transition to {to_status}")
    from_status = record.status
    record.status = to_status
    if to_status == ExceptionStatus.RESOLVED:
        record.resolved_at = instant
    session.add(
        ExceptionHistory(
            exception_id=record.id,
            from_status=from_status,
            to_status=to_status,
            changed_at=instant,
            actor=actor_value,
            transition_reason=reason_value,
        )
    )
    session.flush()
    return record


class ExceptionLifecycle:
    """Object-oriented lifecycle entry point for service/API layers."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def transition(
        self,
        exception_id: int,
        to_status: ExceptionStatus,
        *,
        actor: str,
        reason: str | None = None,
        changed_at: datetime | None = None,
    ) -> ExceptionRecord:
        return transition_exception(
            self.session,
            exception_id,
            to_status,
            actor=actor,
            reason=reason,
            changed_at=changed_at,
        )


__all__ = ["ExceptionLifecycle", "InvalidTransition", "LEGAL_TRANSITIONS", "transition_exception"]
