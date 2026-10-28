"""Transactional delivery through HTTPS. Two separate, idempotent sends."""
import os, re, json, uuid, hashlib, base64, time, threading
from collections import defaultdict, deque
from urllib.parse import urlparse
import requests
from engine import ValidationError, calculate
from reports import pdf_data, xlsx_data
SPWM='webmaster@spwm.co.za'
LOCK=threading.Lock();COUNTS=defaultdict(deque)

class DeliveryError(Exception):
    def __init__(self,message,client_accepted=False):super().__init__(message);self.client_accepted=client_accepted

def public_url():return (os.getenv('PUBLIC_URL') or os.getenv('RENDER_EXTERNAL_URL') or '').rstrip('/')

def configured():return os.getenv('ENABLE_EMAIL','false').lower()=='true' and all(os.getenv(k) for k in ('FROM_EMAIL','RESEND_API_KEY','TURNSTILE_SITE_KEY','TURNSTILE_SECRET_KEY','PRIVACY_URL')) and bool(public_url())

def validate_person(p):
    if not isinstance(p,dict) or p.get('consent') is not True:raise ValidationError('Report consent and contact details are required.')
    out={k:str(p.get(k,'')).strip() for k in ('name','email','mobile')}
    if not 1<=len(out['name'])<=120 or len(out['mobile'])>30 or any(ord(x)<32 for x in ''.join(out.values())):raise ValidationError('Invalid contact details.')
    if len(out['email'])>254 or not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+',out['email']):raise ValidationError('Enter a valid email.')
    out['followup']=p.get('followup') is True;return out

def verify_bot(token):
    if not isinstance(token,str) or not 1<=len(token)<=2048:return False
    r=requests.post('https://challenges.cloudflare.com/turnstile/v0/siteverify',data={'secret':os.environ['TURNSTILE_SECRET_KEY'],'response':token},timeout=15);r.raise_for_status();d=r.json()
    return d.get('success') is True and d.get('hostname')==urlparse(public_url()).hostname and d.get('action')=='report'

def allowed(email):
    now=time.time();key=hashlib.sha256(email.lower().encode()).hexdigest()
    with LOCK:
        for k in list(COUNTS):
            while COUNTS[k] and COUNTS[k][0]<now-86400:COUNTS[k].popleft()
            if not COUNTS[k]:del COUNTS[k]
        if len(COUNTS)>5000:return False
        if sum(x>now-3600 for x in COUNTS[key])>=3 or len(COUNTS['global'])>=40:return False
        COUNTS[key].append(now);COUNTS['global'].append(now);return True

def send_reports(loan,person,request_id):
    reference=str(uuid.UUID(request_id));r=calculate(loan);person=validate_person(person)
    pdf=pdf_data(loan,r,person,reference);xlsx=xlsx_data(loan,r,person,reference)
    fingerprint=hashlib.sha256(json.dumps({'loan':loan,'person':person,'reference':reference},sort_keys=True).encode()).hexdigest()
    def attachment(name,data):return {'filename':name,'content':base64.b64encode(data).decode()}
    def send(role,to,files,body):
        payload={'from':os.environ['FROM_EMAIL'],'to':[to],'reply_to':SPWM,'subject':('Your SPWM Loan Payoff Summary' if role=='client' else 'SPWM report copy and Excel version')+' — '+reference,'text':body,'attachments':files}
        response=requests.post('https://api.resend.com/emails',headers={'Authorization':'Bearer '+os.environ['RESEND_API_KEY'],'Idempotency-Key':fingerprint+'-'+role},json=payload,timeout=25);response.raise_for_status();return response.json().get('id')
    text=f"Dear {person['name']},\n\nYour requested estimate is attached. A PDF copy is also sent to SPWM. This is not a lender settlement quotation.\nReference: {reference}\n\nSP Wealth Management\n{SPWM}"
    client_accepted=False
    try:
        client_id=send('client',person['email'],[attachment('SPWM-summary.pdf',pdf)],text);client_accepted=True
        spwm_id=send('spwm',SPWM,[attachment('SPWM-summary.pdf',pdf),attachment('SPWM-calculation.xlsx',xlsx)],f"Requested report copy.\nClient: {person['name']}\nEmail: {person['email']}\nMobile: {person['mobile']}\nSpecific follow-up requested: {person['followup']}\nReference: {reference}\nInternal Excel version must not be forwarded automatically to the client.")
    except requests.RequestException as e:raise DeliveryError('Client accepted; SPWM copy not confirmed. Retry unchanged with a fresh security check.' if client_accepted else 'Sending not confirmed. Retry unchanged with a fresh security check, or contact SPWM.',client_accepted) from e
    return dict(reference=reference,client_id=client_id,spwm_id=spwm_id)
