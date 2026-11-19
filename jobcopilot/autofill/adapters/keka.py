"""Keka adapter (<slug>.keka.com/careers), declarative: specs/keka.yaml.

A Keka posting page (/careers/jobdetails/<id>) is only the description; the
form lives at /careers/applyjob/<id>, which is what autofill opens
(service.application_url). If a run still lands on the description page, the
executor opens the form through SafePage.open_application (Keka's
"Apply for this job" is a typeless button outside any form).

Keka specifics handled here:
  * the résumé is uploaded to Keka's server the moment it is picked, which the
    fill guard blocks, and the form ends in an image CAPTCHA: both are left for
    the person (spec `yours`), so a Keka run ends as needs_you, never ready;
  * the consent checkbox's text sits in a <span>, so it is named in `never`;
  * read-only date pickers and the unlabelled widget mirrors (the "Years" /
    "Months" boxes) are never typed: a required date picker is yours to pick;
  * a sign-in wall or an email OTP step pauses the run as needs_you.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from ..plan import FieldAction
from .base import register
from .greenhouse import SpecAdapter

_OTP_RE = re.compile(r"\b(otp|one[- ]time (password|passcode|code)|verification code|verify (your )?email)\b", re.I)
_TEXTLIKE = {"text", "email", "tel", "url", "number", "date", "search", ""}


def application_url(url: str) -> str:
    """The form URL for a Keka posting URL: /careers/jobdetails/<id> ->
    /careers/applyjob/<id> (same host, query and fragment dropped). Any other
    URL is returned unchanged."""
    m = re.match(r"^(https://[A-Za-z0-9-]+\.keka\.com)/careers/jobdetails/(\d+)/?(?:[?#].*)?$", url or "")
    return f"{m.group(1)}/careers/applyjob/{m.group(2)}" if m else url


@register
class KekaAdapter(SpecAdapter):
    spec_name = "keka"

    def resolve_field(self, f: dict, ctx: Any, *, step: Optional[str] = None) -> Optional[FieldAction]:
        meta = f.get("meta") or {}
        typ = f.get("type") or "text"
        entry = self._spec_entry(f, step)
        if entry and entry.get("question") and (f.get("label") or "") == (f.get("name") or ""):
            f = dict(f, label=entry["question"])   # Keka's label[for] points elsewhere: use the spec's
        if typ in _TEXTLIKE and not (f.get("label") or f.get("name") or f.get("id")):
            return None                    # a widget's display box ("Years" / "Months"): not a question
        if typ in _TEXTLIKE and meta.get("readonly") and self._yours_note(f) is None \
                and not self._matches_any(f, self.spec.get("never") or []):
            if not f.get("required"):
                return None                # an optional read-only picker: left alone
            label = f.get("label") or f.get("name") or ""
            return FieldAction(f.get("locator") or "", label, "fill", None, "yours", 0.0, True,
                               f"pick {label or 'the date'} in the form's date picker", True)
        return super().resolve_field(f, ctx, step=step)

    def _blocker(self, safe: Any) -> Optional[str]:
        why = super()._blocker(safe)
        if why:
            return why
        try:
            labels = " ".join(str(f.get("label") or f.get("placeholder") or "") for f in safe.fields()
                              if not f.get("hidden"))
        except Exception:  # noqa: BLE001
            return None
        if _OTP_RE.search(labels):
            return "Keka asks for an email OTP / verification code: do that step yourself, then run again"
        return None


__all__ = ["KekaAdapter", "application_url"]
