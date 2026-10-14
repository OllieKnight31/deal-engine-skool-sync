"""
Which contacts are the team's own test traffic, so no sync copies them into Close.

Added 14 Oct 2026, when Ollie had seven test records deleted from both CRMs ("Charlie
Testing", "YouTube test Booking Charlie DELETE", zz…@example.com e2e probes and so on) and
asked that the syncs stop recreating them. Deliberately narrow: real Skool members called
Twyla Estell <testellappleid@…> and David T <david.tester0123@…> must not match, so "test"
only counts as a whole word in the NAME, never as a substring of an email.
"""
import re

_EMAIL = re.compile(r"(@example\.(com|org)$|^zz|^__verify__|^charlietest@|^charlieknight\d+@"
                    r"|^test@|^resource-library-test@|\+(cal)?test@|\+detest@)", re.I)
_NAME = re.compile(r"(^|\W)(test|testing|zze2e|__verify__)(\W|$)", re.I)


def is_test_record(name, email):
    return bool(_EMAIL.search((email or "").strip()) or _NAME.search(name or ""))
