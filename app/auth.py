from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class AuthConfig:
    doctor_pins: Dict[str, str]
    admin_pin: str


def check_pin(expected: str, provided: str) -> bool:
    return str(provided or "").strip() == str(expected or "").strip()


def doctor_login(cfg: AuthConfig, doctor: str, pin: str) -> bool:
    expected = cfg.doctor_pins.get(doctor)
    if expected is None:
        return False
    return check_pin(expected, pin)


def admin_login(cfg: AuthConfig, pin: str) -> bool:
    return check_pin(cfg.admin_pin, pin)
