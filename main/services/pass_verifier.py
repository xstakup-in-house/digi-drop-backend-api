from decimal import Decimal
from django.db import transaction
from web3 import Web3
from django.conf import settings
from main.models import PassTransaction, DigiPass, DigiUser

w3 = Web3(Web3.HTTPProvider(settings.BSC_RPC))

CONFIRMATIONS_REQUIRED = 5

def _is_confirmed(receipt):
    current_block = w3.eth.block_number
    return current_block - receipt.blockNumber >= CONFIRMATIONS_REQUIRED

def process_mint_event(event):
    tx_hash = event["transactionHash"].hex()

    receipt = w3.eth.get_transaction_receipt(tx_hash)
    if receipt.status != 1 or not _is_confirmed(receipt):
        return  # safety check

    args = event["args"]
    wallet = args["user"]
    pass_id = args["passId"]
    amount_paid = Decimal(w3.from_wei(args["amountPaid"], "ether"))

    with transaction.atomic():
        if PassTransaction.objects.filter(tx_hash=tx_hash).exists():
            return  # idempotent

        user = DigiUser.objects.get(wallet_address__iexact=wallet)
        digipass = DigiPass.objects.get(pass_id=pass_id)

        PassTransaction.objects.create(
            tx_hash=tx_hash,
            user=user,
            wallet_address=wallet,
            digipass=digipass,
            minted=True,
            is_verified=True,
            amount_paid_bnb=Decimal(amount_paid),
            usd_price=digipass.usd_price,
            is_upgrade=False,
        )

        profile = user.profile
        profile.current_pass = digipass
        profile.has_pass = True
        profile.save(update_fields=["current_pass", "has_pass"])


def process_upgrade_event(event):
    tx_hash = event["transactionHash"].hex()
    receipt = w3.eth.get_transaction_receipt(tx_hash)

    if receipt.status != 1 or not _is_confirmed(receipt):
        return

    args = event["args"]
    wallet = args["user"]
    old_pass_id = args["oldPassId"]
    new_pass_id = args["newPassId"]
    amount_paid = Decimal(w3.from_wei(args["amountPaid"], "ether"))

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
