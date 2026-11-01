"""Regular reconciliation with Razorpay, in case a webhook never arrived. A failing run is
logged, never fatal."""

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from services.assistant_payment_service import AssistantPaymentService

logger = logging.getLogger("webnest.assistant.payments")

FIRST_RUN_DELAY = timedelta(minutes=5)


async def reconcile_payments(payments: AssistantPaymentService) -> None:
    try:
        granted = await payments.reconcile()
        if granted:
            logger.info("Razorpay reconciliation granted %s passes", granted)
    except Exception as exc:
        logger.error("Razorpay reconciliation failed: %s", type(exc).__name__)


def register_payment_jobs(scheduler: AsyncIOScheduler, payments: AssistantPaymentService, keys_configured: bool) -> None:
    """Without keys there is nothing to ask Razorpay with, so nothing to schedule."""
    if not keys_configured:
        return
    scheduler.add_job(
        reconcile_payments,
        IntervalTrigger(minutes=15),
        args=[payments],
        id="assistant_payment_reconcile",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc) + FIRST_RUN_DELAY,
        misfire_grace_time=600,
        coalesce=True,
        max_instances=1,
    )
