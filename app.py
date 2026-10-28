"""SPWM hosted calculator. Run with gunicorn app:app; no debug mode."""
import os
from pathlib import Path
import requests
from flask import Flask, request, jsonify, render_template_string, Response, send_file
from engine import calculate, ValidationError
from reports import csv_data
from interface import HTML
import mailer
app=Flask(__name__);app.config['MAX_CONTENT_LENGTH']=100000

@app.before_request
def check_origin():
    if request.method=='POST':
        expected=mailer.public_url() or request.host_url.rstrip('/')
        if not request.is_json or request.headers.get('Origin')!=expected:return jsonify(error='Invalid request origin or content type.'),403

@app.after_request
def secure_headers(response):
    response.headers['Cache-Control']='no-store';response.headers['X-Content-Type-Options']='nosniff';response.headers['Referrer-Policy']='strict-origin-when-cross-origin'
    response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self' 'unsafe-inline' https://challenges.cloudflare.com; frame-src https://challenges.cloudflare.com; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self' https://challenges.cloudflare.com; base-uri 'self'; form-action 'self'; frame-ancestors 'self' https://www.spwm.co.za https://spwm.co.za"
    return response

@app.get('/')
def index():return render_template_string(HTML,enabled=mailer.configured(),sitekey=os.getenv('TURNSTILE_SITE_KEY',''),privacy=os.getenv('PRIVACY_URL',''))

@app.get('/logo')
def logo():return send_file(Path(__file__).with_name('logo.jpg'),mimetype='image/jpeg')

@app.get('/health')
def health():return jsonify(status='ok',version='3.0.0',email_configured=mailer.configured())

@app.post('/calculate')
def calc():
    try:return jsonify(calculate(request.get_json()['loan']))
    except (ValidationError,ValueError,TypeError,KeyError,AttributeError,OverflowError) as e:return jsonify(error=str(e)),400

@app.post('/csv')
def export():
    try:return Response(csv_data(calculate(request.get_json()['loan'])),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=SPWM-schedule.csv'})
    except (ValidationError,ValueError,TypeError,KeyError,AttributeError,OverflowError) as e:return jsonify(error=str(e)),400

@app.post('/email')
def email():
    if not mailer.configured():return jsonify(error='Email reporting is not configured. Contact webmaster@spwm.co.za.'),503
    try:
        d=request.get_json();p=mailer.validate_person(d.get('person'));calculate(d['loan'])
        if not mailer.verify_bot(d.get('token')):return jsonify(error='Please complete a fresh security check.'),400
        if not mailer.allowed(p['email']):return jsonify(error='Request limit reached. Contact webmaster@spwm.co.za.'),429
        result=mailer.send_reports(d['loan'],d['person'],d.get('request_id'))
        return jsonify(reference=result['reference'],message='Email service accepted your PDF for sending and the SPWM PDF/Excel copy. Check your inbox/spam; acceptance is not guaranteed inbox delivery.')
    except mailer.DeliveryError as e:return jsonify(error=str(e),client_accepted=e.client_accepted),503
    except requests.RequestException:return jsonify(error='Security/email service temporarily unavailable. Please retry with a fresh security check.'),503
    except (ValidationError,ValueError,TypeError,KeyError,AttributeError,OverflowError):return jsonify(error='Check the contact details, request reference and calculation inputs.'),400
    except Exception:
        app.logger.error('Report processing failed; personal data not logged.')
        return jsonify(error='Report processing failed. Please contact webmaster@spwm.co.za.'),500
