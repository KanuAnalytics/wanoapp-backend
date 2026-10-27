"""Blocks disposable / temporary email addresses at account creation.

Throwaway inbox services let one person mint unlimited accounts that still
pass the email OTP (the inbox really does receive the code). The September
2026 bot wave signed up ~660 accounts this way on maxxspace.com and
uberip.com, so every path that creates an account from an email address
checks it here first.

The domain list is bundled from the community-maintained
disposable-email-domains project (see the header of the data file for how to
refresh it). Domains it misses can be added to EXTRA_BLOCKED_DOMAINS.
"""

from functools import lru_cache
from pathlib import Path
from typing import FrozenSet, Optional

from fastapi import HTTPException, status

_BLOCKLIST_PATH = Path(__file__).parent / "data" / "disposable_email_blocklist.conf"

# Wano-specific additions on top of the bundled list. Both of these are
# already in the upstream list; they're repeated here so the bot-wave domains
# stay blocked even if a future refresh of the data file were to drop them.
EXTRA_BLOCKED_DOMAINS = frozenset({
    "maxxspace.com",
    "uberip.com",
})

DISPOSABLE_EMAIL_MESSAGE = (
    "Temporary or disposable email addresses can't be used to create a Wano "
    "account. Please sign up with a regular email provider such as Gmail, "
    "Outlook or Yahoo."
)


@lru_cache(maxsize=1)
def _blocked_domains() -> FrozenSet[str]:
    domains = set(EXTRA_BLOCKED_DOMAINS)
    with _BLOCKLIST_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip().lower()
            if line and not line.startswith("#"):
                domains.add(line)
    return frozenset(domains)


def is_disposable_email(email: Optional[str]) -> bool:
    """True when the address's domain, or any parent domain of it, is blocked.

    Parent domains are checked so subdomains like ``inbox.uberip.com`` are
    caught too. The bare top-level domain is never checked on its own, so a
    list entry can't accidentally block a whole TLD.
    """
    if not email or "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1].strip().lower().rstrip(".")
    if not domain:
        return False
    blocked = _blocked_domains()
    labels = domain.split(".")
    return any(".".join(labels[i:]) in blocked for i in range(len(labels) - 1))


def ensure_not_disposable_email(email: Optional[str]) -> None:
    """Raise a 400 with a user-facing message for a disposable address."""
    if is_disposable_email(email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=DISPOSABLE_EMAIL_MESSAGE,
        )
