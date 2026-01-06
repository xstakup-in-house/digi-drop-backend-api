import requests
from web3 import Web3
from .models import DigiUser, PassTransaction, DigiPass
from decimal import Decimal
from django.db import transaction
from django.core.cache import cache
from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.utils.http import urlsafe_base64_encode
from django.core.mail import EmailMessage
from django.template.loader import render_to_string
from django.utils.encoding import force_bytes








COINLORE_API = "https://api.coinlore.net/api/ticker/?id=2710"  # 2710 = BNB

def get_bnb_usd_price() -> Decimal:
    cached_price = cache.get("bnb_usd_price")
    if cached_price:
        return Decimal(cached_price)

    try:
        resp = requests.get(COINLORE_API, timeout=5)
        resp.raise_for_status()
        data = resp.json()[0]  # Coinlore returns a list
        price_usd = Decimal(str(data["price_usd"]))
    except Exception as e:
        raise RuntimeError(f"Failed to fetch BNB price: {e}")

    # cache for 5 min
    cache.set("bnb_usd_price", str(price_usd), timeout=180)
    return price_usd

def send_email(subject, html_message, email):
    email = EmailMessage(
                subject.replace('\n', '').replace('\r', ''),
                html_message,
                settings.DEFAULT_FROM_EMAIL,  # from email
                [email]  # to email
            )
    email.content_subtype = "html" 
    email.send()

def send_email_verification_link(user_id):
    user = DigiUser.objects.get(id=user_id)
    uid=urlsafe_base64_encode(force_bytes(user.id))
    token = default_token_generator.make_token(user)
    # url=reverse('email_activation', kwargs={'uidb64':uid, 'token':token})
    url=f"/email-verification/{uid}/{token}" #frontend url
    frontend_domain="localhost:3000"
    context={
        'protocol':'http',
        'domain':frontend_domain,
        'url':url,
        'site_name':settings.SITE_NAME
    }
    email_subject=f"Email Verification for {settings.SITE_NAME}"
    email_html_message = render_to_string('email_template.html', context)
    send_email(email_subject, email_html_message, user)

def send_reset_password_email(user_id):
    user = DigiUser.objects.get(id=user_id)
    uidb64=urlsafe_base64_encode(force_bytes(user.pkid))
    token = default_token_generator.make_token(user)
    frontend_domain='localhost:3000'
    # relative_link =reverse('reset-password-confirm', kwargs={'uidb64':uidb64, 'token':token})
    frontend_link=f"/password-reset/{uidb64}/{token}"
    context={
        'protocol':'http',
        'domain':frontend_domain,
        'url':frontend_link,
        'site_name':settings.SITE_NAME
    }
    email_subject=f"Password Reset Request"
    email_html_message = render_to_string('email_reset.html', context)
    send_email(email_subject, email_html_message,  user)
