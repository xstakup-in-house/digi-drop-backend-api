from decimal import Decimal
from django.db import transaction
from web3 import Web3
from django.conf import settings
from main.models import PassTransaction, DigiPass, DigiUser

w3 = Web3(Web3.HTTPProvider(settings.BSC_RPC))

CONFIRMATIONS_REQUIRED = 5

from decimal import Decimal
from django.db import transaction
from main.models import DigiUser, DigiPass, PassTransaction


def handle_pass_minted(event):
    tx_hash = event["transactionHash"]
    args = event["decodedEvent"]["params"]

    wallet = args["user"]["value"]
    pass_id = int(args["passId"]["value"])
    amount_paid = Decimal(args["amountPaid"]["value"]) / Decimal(10**18)

    with transaction.atomic():
        if PassTransaction.objects.filter(tx_hash=tx_hash).exists():
            return

        user = DigiUser.objects.get(wallet_address__iexact=wallet)
        digipass = DigiPass.objects.get(pass_id=pass_id)

        PassTransaction.objects.create(
            tx_hash=tx_hash,
            user=user,
            wallet_address=wallet,
            digipass=digipass,
            minted=True,
            is_verified=True,
            amount_paid_bnb=amount_paid,
            usd_price=digipass.usd_price,
            is_upgrade=False,
        )

        profile = user.profile
        profile.current_pass = digipass
        profile.has_pass = True
        profile.save(update_fields=["current_pass", "has_pass"])


def handle_pass_upgraded(event):
    tx_hash = event["transactionHash"]
    args = event["decodedEvent"]["params"]

    wallet = args["user"]["value"]
    new_pass_id = int(args["newPassId"]["value"])
    amount_paid = Decimal(args["amountPaid"]["value"]) / Decimal(10**18)

    with transaction.atomic():
        if PassTransaction.objects.filter(tx_hash=tx_hash).exists():
            return

        user = DigiUser.objects.get(wallet_address__iexact=wallet)
        new_pass = DigiPass.objects.get(pass_id=new_pass_id)

        PassTransaction.objects.create(
            tx_hash=tx_hash,
            user=user,
            wallet_address=wallet,
            digipass=new_pass,
            minted=True,
            is_verified=True,
            amount_paid_bnb=amount_paid,
            usd_price=new_pass.usd_price,
            is_upgrade=True,
        )

        profile = user.profile
        profile.current_pass = new_pass
        profile.save(update_fields=["current_pass"])


