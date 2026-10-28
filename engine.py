"""SPWM 3.0.0 educational loan model. Not a lender settlement quote."""
import calendar, math
from datetime import date
class ValidationError(ValueError): pass

def num(value,label,minimum=0,maximum=1e10):
    try:
        if value is None or value=='' or isinstance(value,bool): raise ValueError()
        n=float(value)
        if not math.isfinite(n) or not minimum<=n<=maximum: raise ValueError()
        return n
    except (ValueError,TypeError): raise ValidationError(label+' is invalid.')

def day(value):
    try:
        d=date.fromisoformat(str(value))
        if d.isoformat()!=value: raise ValueError()
        return d
    except (ValueError,TypeError): raise ValidationError('Use valid YYYY-MM-DD dates.')

def add_month(d,n):
    y,m=divmod(d.year*12+d.month-1+n,12);m+=1
    return date(y,m,min(d.day,calendar.monthrange(y,m)[1]))

def calculate(c,baseline=False):
    if not isinstance(c,dict): raise ValidationError('Invalid loan details.')
    kind=c.get('kind');p=num(c.get('principal'),'Principal',.01);rate=num(c.get('rate'),'Annual rate',0,100)/100
    if kind not in ('personal','vehicle','home'): raise ValidationError('Select a loan type.')
    term=num(c.get('months'),'Months',1,600)
    if term!=int(term): raise ValidationError('Months must be whole numbers.')
    term=int(term);start=day(c.get('start'));first=day(c.get('first'))
    if not 0<(first-start).days<=62: raise ValidationError('First payment must be 1–62 days after balance date.')
    vat=num(c.get('vat',.15),'VAT fraction',0,.3);service=num(c.get('service',0),'Service fee');init=num(c.get('initiation',0),'Initiation fee')
    cap=min(.15*p,min(5250,1100+.1*max(p-10000,0)) if kind=='home' else min(1050,165+.1*max(p-1000,0)))*(1+vat)
    if service>60*(1+vat)+.005: raise ValidationError('Service fee exceeds configured reference limit.')
    if init>cap+.005: raise ValidationError('Initiation fee exceeds configured reference limit.')
    extra=0 if baseline else num(c.get('extra',0),'Monthly extra')
    balloon=num(c.get('balloon',0),'Balloon',0,p)
    if balloon and kind!='vehicle': raise ValidationError('Balloon is for vehicle finance only.')
    mode=c.get('insurance','none');ir=num(c.get('insurance_rate',0),'Insurance rate');fixed=num(c.get('premium',0),'Premium');ic=2 if kind=='home' else 4.5
    if mode not in ('none','fixed','reducing'): raise ValidationError('Invalid insurance method.')
    if (mode=='reducing' and ir>ic) or (mode=='fixed' and fixed>p/1000*ic+.005): raise ValidationError('Credit-life premium exceeds base reference screen. Enhanced policies require separate review.')
    warnings=['Reference limits require current legal review. Actual lender billing, day count and rounding may differ.']
    if mode=='fixed': warnings.append('Fixed insurance premium requires ongoing review as debt reduces.')
    if c.get('repo') not in ('',None):
        limit=num(c['repo'],'Repo',0,100)+(12 if kind=='home' else 21 if kind=='personal' else 17)
        if rate*100>limit+1e-8: raise ValidationError('Annual interest exceeds configured repo-linked reference screen.')
    else: warnings.append('Interest ceiling not screened: no verified applicable repo rate entered.')
    quote=day(c['quote']) if c.get('quote') else None;notice=day(c['notice']) if c.get('notice') else None
    if quote and quote<start: raise ValidationError('Settlement cannot precede balance date.')
    if notice and quote and notice>quote: raise ValidationError('Notice cannot follow settlement.')
    if c.get('rate_type','variable') not in ('variable','fixed'): raise ValidationError('Invalid rate type.')
    events={}
    def event(d):return events.setdefault(d,dict(regular=False,extra=0.,fees=[]))
    for i in range(term):event(add_month(first,i))['regular']=True
    maturity=add_month(first,term-1)
    irregular=[] if baseline else c.get('irregular',[]);fees=c.get('fees',[])
    if not isinstance(irregular,list) or not isinstance(fees,list) or len(irregular)>100 or len(fees)>100:raise ValidationError('Use at most 100 dated entries per section.')
    for x in irregular:
        if not isinstance(x,dict):raise ValidationError('Invalid dated payment.')
        d=day(x.get('date'))
        if not start<=d<=maturity:raise ValidationError('Dated payment outside loan period.')
        event(d)['extra']+=num(x.get('amount'),'Dated payment',.01)
    for x in fees:
        if not isinstance(x,dict):raise ValidationError('Invalid dated fee.')
        d=day(x.get('date'))
        if x.get('kind') not in ('insurance','default','collection') or x.get('verified') is not True:raise ValidationError('Independently review entitlement and tariffs for dated fees.')
        if not start<=d<=maturity:raise ValidationError('Dated fee outside loan period.')
        event(d)['fees'].append((x['kind'],num(x.get('amount'),'Dated fee',.01)))
    if fees:warnings.append('Dated fees are supplied amounts, not certified lawful. No default ledger, collection tariff or section 103(5) reconstruction is performed.')
    if quote:event(quote)
    capital=p+(init if c.get('finance') is True else 0);r=rate/12
    payment=num(c['payment'],'Actual instalment',.01) if c.get('payment') not in ('',None) else (capital-balloon)/term if not r else (capital*(1+r)**term-balloon)*r/((1+r)**term-1)
    interest=fee_balance=paid=total_i=total_ins=unused=0.;total_f=init;previous=start;rows=[];payoff=None;q=None
    def allocate(d,label,opening,accr,charges,ins,offered):
        nonlocal capital,interest,fee_balance,paid,unused
        ip=min(offered,interest);interest-=ip;offered-=ip
        fp=min(offered,fee_balance);fee_balance-=fp;offered-=fp
        cp=min(offered,capital);capital-=cp;offered-=cp
        actual=ip+fp+cp;paid+=actual;unused+=offered
        rows.append(dict(date=d.isoformat(),event=label,opening=opening,interest=accr,fees=charges,insurance=ins,payment=actual,interest_paid=ip,fees_paid=fp,capital_paid=cp,principal=max(0,capital),balance=max(0,capital+interest+fee_balance),unused=offered))
    if init and c.get('finance') is not True:fee_balance=init;allocate(start,'Initiation paid upfront',capital,0,init,0,init)
    for d,e in sorted(events.items()):
        active=capital+interest+fee_balance>1e-7;opening=capital
        accr=capital*rate*(d-previous).days/365 if active else 0.;interest+=accr;total_i+=accr;previous=d;charges=ins=0.
        if active:
            for category,amount in e['fees']:
                charges+=amount
                if category=='insurance':ins+=amount
            if e['regular']:
                premium=capital/1000*ir if mode=='reducing' else fixed if mode=='fixed' else 0
                charges+=service+premium;ins+=premium
        fee_balance+=charges;total_f+=charges;total_ins+=ins
        offered=(payment+extra+charges if active and e['regular'] else 0)+e['extra'];final=e['regular'] and d==maturity
        if active and final:offered=max(offered,capital+interest+fee_balance)
        if active or offered:allocate(d,'Final incl. balloon/residual' if final else 'Scheduled' if e['regular'] else 'Dated event',opening,accr,charges,ins,offered)
        if active and capital+interest+fee_balance<1e-7 and payoff is None:payoff=d.isoformat()
        if d==quote:
            debt=max(0,capital+interest+fee_balance);large=kind=='home' or p>=250000 or c.get('original_large') is True;termination=0.
            if large and debt>1e-7:
                if c.get('rate_type','variable')=='fixed':termination=None if c.get('fixed_charge') in ('',None) else num(c['fixed_charge'],'Lender-confirmed termination charge')
                else:termination=capital*rate*max(0,(add_month(notice or quote,3)-quote).days)/365
            q=dict(date=d.isoformat(),principal=max(0,capital),interest=max(0,interest),fees=max(0,fee_balance),termination=termination,subtotal=debt,total=None if termination is None else debt+termination)
    result=dict(instalment=payment,payoff=payoff,final_payment=next((x['payment'] for x in reversed(rows) if x['payment']>1e-7),0),total_paid=paid,total_interest=total_i,total_fees=total_f,total_insurance=total_ins,unused=unused,quote=q,warnings=warnings,rows=rows)
    if not baseline:result['interest_saved']=calculate(c,True)['total_interest']-total_i
    return result
