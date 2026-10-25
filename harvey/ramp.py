"""Daily send-limit ramp (owner 2026-10-25: "+10 a day up to 50").

Once a day the email channel's max_daily_sends goes up by usage.ramp_step,
up to usage.ramp_max — but only while the bounce rate stays under
usage.ramp_max_bounce_rate and no reply has been escalated as a complaint.
When either goes over, it holds and records why (setting 'ramp_held').
The new limit is written to harvey.local.yaml (backup kept) and applied to
the running config, so the page shows it and a restart keeps it.
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

import yaml

from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.ramp")

RAMP_SETTING = "send_ramp_date"
HELD_SETTING = "ramp_held"


def _config_path() -> Path:
    local = PROJECT_ROOT / "harvey.local.yaml"
    return local if local.exists() else PROJECT_ROOT / "harvey.yaml"


async def _complaints(state) -> int:
    async with state._connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM conversations WHERE intent = 'escalate'"
        ) as cur:
            return (await cur.fetchone())[0]


async def daily_ramp(state, config, today: str, path: Path | None = None) -> dict:
    usage = config.usage
    step = int(getattr(usage, "ramp_step", 0) or 0)
    top = int(getattr(usage, "ramp_max", 0) or 0)
    if step <= 0 or top <= 0:
        return {"skipped": "off"}
    if await state.get_setting(RAMP_SETTING) == today:
        return {"skipped": "already today"}
    await state.set_setting(RAMP_SETTING, today)

    current = int(config.channels.email.max_daily_sends)
    if current >= top:
        return {"skipped": "at max", "limit": current}

    sent = await state.count_outbox_sent()
    try:
        bounces = int(await state.get_setting("bounce_count") or 0)
    except ValueError:
        bounces = 0
    rate = bounces / sent if sent else 0.0
    limit_rate = float(getattr(usage, "ramp_max_bounce_rate", 0.02) or 0.02)
    complaints = await _complaints(state)
    if rate >= limit_rate or complaints:
        why = (f"bounce rate {rate:.1%} (limit {limit_rate:.0%})" if rate >= limit_rate
               else f"{complaints} complaint repl{'y' if complaints == 1 else 'ies'}")
        await state.set_setting(HELD_SETTING, f"{today}: held at {current} — {why}")
        return {"held": why, "limit": current}

    new = min(top, current + step)
    path = path or _config_path()
    raw = yaml.safe_load(path.read_text()) or {}
    raw.setdefault("channels", {}).setdefault("email", {})["max_daily_sends"] = new
    backup = path.parent / "data" / "config-backups"
    try:
        backup.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup / f"{path.name}.{datetime.now():%Y%m%d-%H%M%S}.ramp.bak")
    except OSError:
        pass
    tmp = path.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
    os.replace(tmp, path)
    config.channels.email.max_daily_sends = new
    await state.set_setting(HELD_SETTING, "")
    logger.info(f"Send limit raised {current} -> {new} (bounce rate {rate:.1%}).")
    return {"raised": [current, new]}
