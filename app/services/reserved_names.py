"""Names only Wano may use ("reserved_names" switch).

Scammers name themselves "Wano Support" and ask for codes. With the switch
on, a username or display name that reads as an official Wano account is
refused at sign-up and when changing names, however it's disguised:
lookalike letters (Cyrillic "о", Greek "α"), digits (W4no), spacing, dots and
underscores, invisible characters, and check marks (✔ ☑ ✅) that imitate the
badge. Only official-sounding names are blocked ("Wano Support", "Team Wano",
or just "Wano" as the whole name); real names that contain it (Wanojiru,
Mwano Help, "wano lover") are fine. Staff accounts are exempt.
Existing accounts are left alone (staff can rename one that's confusing).
"""

import re
import unicodedata
from typing import Optional

from fastapi import HTTPException

SWITCH = "reserved_names"

# Characters that only exist to hide things or fake a badge.
_INVISIBLE = re.compile("[­͏؜ᅟᅠ឴឵᠎​-‏‪-‮"
                        "⁠-⁤⁪-⁯ㅤ﻿ﾠ]")
_CHECKS = re.compile("[✓✔☑✅\U0001F5F8√\U00002705☑️]")
_CONFUSABLE = str.maketrans({
    # Cyrillic and Greek lookalikes
    "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c", "т": "t",
    "у": "y", "х": "x", "ѕ": "s", "і": "i", "ј": "j", "ԁ": "d", "ԝ": "w", "ո": "n", "ν": "v", "α": "a",
    "ο": "o", "ρ": "p", "τ": "t", "ω": "w", "ε": "e", "ι": "i", "κ": "k", "μ": "m", "η": "n",
    # digits and symbols used as letters
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i", "|": "i",
})
WORDS = ("team", "support", "official", "admin", "staff", "help", "helpdesk", "safety", "moderator", "mod",
         "care", "verify", "verified", "security", "payouts", "rewards", "customer")


def _clean(name: str) -> str:
    t = unicodedata.normalize("NFKC", name or "")
    t = _INVISIBLE.sub("", t)
    # CamelCase ("WanoSupport") reads as two words.
    t = re.sub(r"(?<=[a-z\u0430-\u044f])(?=[A-Z\u0410-\u042f])", " ", t)
    t = t.lower().translate(_CONFUSABLE)
    return "".join(ch for ch in unicodedata.normalize("NFKD", t) if not unicodedata.combining(ch))


def tokens(name: str) -> list:
    words = [w for w in re.split(r"[^a-z]+", _clean(name)) if w]
    # "W a n o" spelled out letter by letter becomes one word.
    out, run = [], ""
    for w in words:
        if len(w) == 1:
            run += w
            continue
        if run:
            out.append(run)
            run = ""
        out.append(w)
    if run:
        out.append(run)
    return out


def normalize(name: str) -> str:
    return "".join(tokens(name))


def is_reserved(name: Optional[str]) -> bool:
    if not name:
        return False
    if _CHECKS.search(name):
        return True
    toks = tokens(name)
    if "".join(toks) in ("wano", "wanoafrica", "wanoapp"):
        return True                     # the bare brand as the whole name
    for i, t in enumerate(toks):
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        prev = toks[i - 1] if i > 0 else ""
        for w in WORDS:
            if t in (f"wano{w}", f"{w}wano", f"wanoafrica{w}"):
                return True
            if t in ("wano", "wanoafrica") and (nxt == w or prev == w):
                return True
            if t == "wano" and nxt == "africa" and i + 2 < len(toks) and toks[i + 2] == w:
                return True
    return False


async def check(db, *, username: Optional[str] = None, display_name: Optional[str] = None,
                user_id: Optional[str] = None) -> None:
    from app.services import creator_push
    if not await creator_push.on(db, SWITCH):
        return
    if user_id:
        from bson import ObjectId
        from app.services import staff
        try:
            u = await db.users.find_one({"_id": ObjectId(str(user_id))}, {"user_type": 1, "staff_role": 1, "is_active": 1})
        except Exception:
            u = None
        if staff.is_staff(u):
            return
    for what, v in (("Username", username), ("Display name", display_name)):
        if is_reserved(v):
            raise HTTPException(400, f"{what} looks like an official Wano account. Please pick another.")
