"""Who is making this request.

Three modes, chosen by environment variable:

  IAP_AUDIENCE set
      Deployed behind Google Cloud Identity-Aware Proxy. Every request
      must carry a valid `X-Goog-IAP-JWT-Assertion` signed by Google for
      exactly our backend. The signature is checked, so a forged
      `X-Goog-Authenticated-User-Email` header gets nowhere. This is the
      mode to run in production.

  TRUST_IAP_HEADER=1
      Behind IAP but without JWT verification -- the email header is
      taken at face value. Only safe when the service refuses traffic
      that has not come through the load balancer (Cloud Run ingress set
      to `internal-and-cloud-load-balancing`). Useful if you cannot
      install google-auth, otherwise prefer the mode above.

  neither
      Local development. Identity is whatever the browser puts in
      `X-Actor`, i.e. self-declared and not to be trusted. The UI shows
      a name box in this mode and the signed-in email otherwise.

Find the audience string with:

    gcloud iap web get-iam-policy --help   # or, more directly:
    echo "/projects/$(gcloud projects describe PROJECT --format='value(projectNumber)')\\
/global/backendServices/BACKEND_SERVICE_ID"
"""
import base64
import json
import logging
import os

from fastapi import HTTPException, Request

log = logging.getLogger("samplequeue.auth")

IAP_AUDIENCE = os.environ.get("IAP_AUDIENCE", "").strip()
TRUST_IAP_HEADER = os.environ.get("TRUST_IAP_HEADER", "").strip() in ("1", "true", "yes")
# Comma-separated list, e.g. "broadinstitute.org". Empty means any signed-in user
# IAP lets through, which is already restricted by the IAP IAM policy.
ALLOWED_DOMAINS = [d.strip().lower() for d in
                   os.environ.get("ALLOWED_DOMAINS", "").split(",") if d.strip()]

JWT_HEADER = "X-Goog-IAP-JWT-Assertion"
EMAIL_HEADER = "X-Goog-Authenticated-User-Email"

MODE = "iap-verified" if IAP_AUDIENCE else ("iap-header" if TRUST_IAP_HEADER else "open")


def _strip_prefix(value: str) -> str:
    """IAP sends "accounts.google.com:someone@example.org"."""
    return value.split(":", 1)[1] if ":" in value else value


def _verify_jwt(token: str) -> str:
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    claims = id_token.verify_token(
        token, google_requests.Request(), audience=IAP_AUDIENCE,
        certs_url="https://www.gstatic.com/iap/verify/public_key")
    email = claims.get("email")
    if not email:
        raise ValueError("IAP token carried no email claim")
    return email


def identify(request: Request) -> str:
    """The authenticated email, or '' when running open locally."""
    if IAP_AUDIENCE:
        token = request.headers.get(JWT_HEADER)
        if not token:
            raise HTTPException(401, "missing IAP assertion -- reach this service through the load balancer")
        try:
            email = _verify_jwt(token)
        except Exception as exc:
            log.warning("rejected IAP assertion: %s", exc)
            raise HTTPException(401, "invalid IAP assertion")
    elif TRUST_IAP_HEADER:
        email = _strip_prefix(request.headers.get(EMAIL_HEADER, "")).strip()
        if not email:
            raise HTTPException(401, "no authenticated user on this request")
    else:
        return (request.headers.get("X-Actor") or "").strip()[:120]

    if ALLOWED_DOMAINS and email.rsplit("@", 1)[-1].lower() not in ALLOWED_DOMAINS:
        raise HTTPException(403, "%s is not in an allowed domain" % email)
    return email[:120]


_logged_audience = False


def audience_hint(request: Request) -> str:
    """Read the `aud` claim without verifying anything.

    Exists purely to save you guessing the IAP_AUDIENCE string. The exact
    format differs between IAP-on-Cloud-Run and IAP-behind-a-load-balancer,
    and getting it wrong looks identical to a broken deployment. So: run in
    `TRUST_IAP_HEADER` mode once, read the value IAP actually sent, then set
    IAP_AUDIENCE to it and restart. Never used for an access decision.
    """
    global _logged_audience
    token = request.headers.get(JWT_HEADER)
    if not token:
        return ""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        aud = json.loads(base64.urlsafe_b64decode(payload)).get("aud", "")
    except Exception:
        return ""
    if aud and not _logged_audience:
        _logged_audience = True
        log.warning("IAP assertions on this service carry aud=%s -- "
                    "set IAP_AUDIENCE to that value to verify signatures", aud)
    return aud


def describe(request: Request) -> dict:
    """What the frontend needs to render the identity corner."""
    if MODE == "open":
        return {"mode": "open", "email": "", "editable": True}
    try:
        email = identify(request)
    except HTTPException:
        email = ""
    out = {"mode": MODE, "email": email, "editable": False}
    if MODE == "iap-header":
        out["suggested_audience"] = audience_hint(request)
    return out
