"""Transactional delivery through HTTPS. Three separate, idempotent sends."""
import os, re, json, uuid, hashlib, base64, time, threading
from collections import defaultdict, deque
from urllib.parse import urlparse
import requests
import logging
from engine import ValidationError, calculate
from reports import pdf_data, xlsx_data, csv_data

SPWM = 'webmaster@spwm.co.za'
REPORTS = 'reports@spwm.co.za'
LOCK = threading.Lock()
COUNTS = defaultdict(deque)


class DeliveryError(Exception):
    def __init__(self, message, client_accepted=False):
        super().__init__(message)
        self.client_accepted = client_accepted


def public_url():
    return (os.getenv('PUBLIC_URL') or os.getenv('RENDER_EXTERNAL_URL') or '').rstrip('/')


def configured():
    return (
        os.getenv('ENABLE_EMAIL', 'false').lower() == 'true'
        and all(os.getenv(k) for k in (
            'FROM_EMAIL', 'RESEND_API_KEY', 'TURNSTILE_SITE_KEY',
            'TURNSTILE_SECRET_KEY', 'PRIVACY_URL'
        ))
        and bool(public_url())
    )


def validate_person(p):
    if not isinstance(p, dict) or p.get('consent') is not True:
        raise ValidationError('Report consent and contact details are required.')

    out = {k: str(p.get(k, '')).strip() for k in ('name', 'email', 'mobile')}

    if not 1 <= len(out['name']) <= 120 or len(out['mobile']) > 30 or any(
        ord(x) < 32 for x in ''.join(out.values())
    ):
        raise ValidationError('Invalid contact details.')

    if len(out['email']) > 254 or not re.fullmatch(
        r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', out['email']
    ):
        raise ValidationError('Enter a valid email.')

    out['followup'] = p.get('followup') is True
    return out


def verify_bot(token):
    log = logging.getLogger('gunicorn.error')

    if not isinstance(token, str) or not 1 <= len(token) <= 2048:
        log.warning(
            'Turnstile rejected: token missing or invalid length. type=%s length=%s',
            type(token).__name__,
            len(token) if isinstance(token, str) else 'n/a',
        )
        return False

    r = requests.post(
        'https://challenges.cloudflare.com/turnstile/v0/siteverify',
        data={
            'secret': os.environ['TURNSTILE_SECRET_KEY'],
            'response': token,
        },
        timeout=15,
    )
    r.raise_for_status()
    d = r.json()

    expected_hostname = urlparse(public_url()).hostname
    ok = (
        d.get('success') is True
        and d.get('hostname') == expected_hostname
        and d.get('action') == 'report'
    )

    if not ok:
        log.warning(
            'Turnstile rejected: success=%s hostname=%s expected_hostname=%s '
            'action=%s error_codes=%s',
            d.get('success'),
            d.get('hostname'),
            expected_hostname,
            d.get('action'),
            d.get('error-codes'),
        )

    return ok


def allowed(email):
    now = time.time()
    key = hashlib.sha256(email.lower().encode()).hexdigest()

    with LOCK:
        for k in list(COUNTS):
            while COUNTS[k] and COUNTS[k][0] < now - 86400:
                COUNTS[k].popleft()
            if not COUNTS[k]:
                del COUNTS[k]

        if len(COUNTS) > 5000:
            return False

        if sum(x > now - 3600 for x in COUNTS[key]) >= 3 or len(COUNTS['global']) >= 40:
            return False

        COUNTS[key].append(now)
        COUNTS['global'].append(now)
        return True


def send_reports(loan, person, request_id):
    reference = str(uuid.UUID(request_id))
    r = calculate(loan)
    person = validate_person(person)

    pdf = pdf_data(loan, r, person, reference)
    xlsx = xlsx_data(loan, r, person, reference)
    csv = csv_data(r)

    fingerprint = hashlib.sha256(
        json.dumps(
            {'loan': loan, 'person': person, 'reference': reference},
            sort_keys=True,
        ).encode()
    ).hexdigest()

    def attachment(name, data):
        return {
            'filename': name,
            'content': base64.b64encode(data).decode(),
        }

    def send(role, to, files, subject, body):
        payload = {
            'from': os.environ['FROM_EMAIL'],
            'to': [to],
            'reply_to': SPWM,
            'subject': subject + ' — ' + reference,
            'text': body,
            'attachments': files,
        }

        response = requests.post(
            'https://api.resend.com/emails',
            headers={
                'Authorization': 'Bearer ' + os.environ['RESEND_API_KEY'],
                'Idempotency-Key': fingerprint + '-' + role,
            },
            json=payload,
            timeout=25,
        )
        response.raise_for_status()
        return response.json().get('id')

    client_text = (
        f"Dear {person['name']},\n\n"
        "Your requested SPWM Loan Payoff Summary is attached.\n\n"
        "This is an educational planning estimate. It is not a lender "
        "settlement quotation, financial advice, credit approval or legal "
        "certification.\n\n"
        f"Reference: {reference}\n\n"
        "SP Wealth Management\n"
        f"{SPWM}"
    )

    spwm_text = (
        "Requested report copy.\n"
        f"Client: {person['name']}\n"
        f"Email: {person['email']}\n"
        f"Mobile: {person['mobile']}\n"
        f"Specific follow-up requested: {person['followup']}\n"
        f"Reference: {reference}\n\n"
        "Internal Excel version must not be forwarded automatically to the client."
    )

    reports_text = (
        "Internal CSV calculation record.\n"
        f"Client: {person['name']}\n"
        f"Email: {person['email']}\n"
        f"Reference: {reference}\n\n"
        "This CSV is for internal record-keeping only and must not be sent to the client."
    )

    accepted = {'client': False, 'spwm': False, 'reports': False}

    try:
        client_id = send(
            'client',
            person['email'],
            [attachment('SPWM-summary.pdf', pdf)],
            'Your SPWM Loan Payoff Summary',
            client_text,
        )
        accepted['client'] = True

        spwm_id = send(
            'spwm',
            SPWM,
            [
                attachment('SPWM-summary.pdf', pdf),
                attachment('SPWM-calculation.xlsx', xlsx),
            ],
            'SPWM report copy and Excel version',
            spwm_text,
        )
        accepted['spwm'] = True

        reports_id = send(
            'reports',
            REPORTS,
            [attachment('SPWM-calculation.csv', csv)],
            'SPWM CSV calculation record',
            reports_text,
        )
        accepted['reports'] = True

    except requests.RequestException as e:
        if accepted['client']:
            message = (
                'Client PDF was accepted for delivery, but one or more internal '
                'SPWM copies were not confirmed. Retry unchanged with a fresh '
                'security check.'
            )
        else:
            message = (
                'Sending not confirmed. Retry unchanged with a fresh security '
                'check, or contact SPWM.'
            )

        raise DeliveryError(message, accepted['client']) from e

    return {
        'reference': reference,
        'client_id': client_id,
        'spwm_id': spwm_id,
        'reports_id': reports_id,
    }
