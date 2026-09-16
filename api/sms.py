"""Sending a text message, and the one message this application sends.

Two backends behind one function, chosen by `SMS_BACKEND` — the same shape
`api/storage.py` uses for uploads, and for the same reason: the caller says
*what* to send and never *how*, so the provider is a deployment decision rather
than a code path anybody has to find.

    console   writes the message to the log. Development only.
    disabled  refuses to send, and says so. The production default.

**There is no third backend yet, and that is the honest state of this.** Sending
an SMS to an Indian number is not a matter of picking a library: the sender id
and every template have to be registered under TRAI's DLT regime before an
operator will deliver them, which is an account, a paper process and a fee. So
what exists here is the whole of the flow *except* the wire — the challenge, the
code, the verification and the tests are real and complete, and `send_otp` is the
seam a provider drops into. `deployment.md` says exactly what has to be done to
fill it.

WHY `disabled` IS THE DEFAULT RATHER THAN `console`
---------------------------------------------------
A `console` backend in production would put a working one-time code in the
application log, where it is readable by anyone with a dashboard login — and it
would *appear to work*, because the endpoint would answer 200 and the customer
would simply never receive anything. `check_production_safety()` refuses to boot
on `console` outside development for that reason, and `disabled` fails loudly at
the one place it matters instead.

WHAT THIS MODULE PROMISES
-------------------------
**It never returns a code to the caller**, and nothing above it ever sees one.
`send_otp` takes the code and returns whether it left the building. A function
that handed the code back would be one refactor away from putting it in a
response body.
"""

from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger("api.sms")


class SmsNotConfigured(RuntimeError):
    """No backend can deliver a message. Raised rather than returned.

    A silent failure here means a customer waiting for a code that was never
    sent, tapping resend, and getting the same silence — so this surfaces as a
    503 from the view rather than as a 200 with nothing behind it.
    """


def send_otp(phone: str, code: str) -> None:
    """Send one verification code to one number.

    `phone` is already normalised to `+91XXXXXXXXXX` by `api/validators.py`;
    every provider wants E.164 and this application has no other spelling.
    """
    backend = settings.SMS_BACKEND

    if backend == "console":
        # Deliberately the whole message rather than just the code: what is
        # being checked in development is usually the wording, and a log line
        # that reads like the text the customer gets is what makes that
        # checkable. Production cannot reach this branch — see the module
        # docstring and check_production_safety().
        logger.warning(
            "SMS (console backend, not sent): %s",
            otp_message(code),
            extra={"phone": phone},
        )
        return

    raise SmsNotConfigured(
        "SMS_BACKEND is 'disabled', so there is nowhere to send a verification "
        "code. Configure a provider — see 'Phone verification' in deployment.md."
    )


def otp_message(code: str) -> str:
    """The text, in one place.

    It will have to be registered with DLT **exactly as written** before an
    Indian operator will deliver it, so editing this string is a paperwork
    change and not only a copy change. Variable parts are the code alone.
    """
    return (
        f"{code} is your eDawr verification code. It expires in "
        f"{settings.OTP_TTL_SECONDS // 60} minutes. Do not share it with anyone."
    )
