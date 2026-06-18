from decimal import Decimal
from django.db import transaction
from web3 import Web3
from django.conf import settings
from main.models import PassTransaction, DigiPass, DigiUser
import logging

logger = logging.getLogger(__name__)

w3 = Web3(Web3.HTTPProvider(settings.BSC_RPC))

CONFIRMATIONS_REQUIRED = 5


def handle_pass_minted(event):
    tx_hash = event["transactionHash"]
    args = event["decodedEvent"]["params"]

    wallet = args["user"]["value"]
    pass_id = int(args["passId"]["value"])
    amount_paid = Decimal(args["amountPaid"]["value"]) / Decimal(10**18)

    try:
        user = DigiUser.objects.get(wallet_address__iexact=wallet)
    except DigiUser.DoesNotExist:
        logger.warning(f"[Webhook] DigiUser with wallet {wallet} does not exist. Skipping pass minted event.")
        return

    try:
        digipass = DigiPass.objects.get(pass_id=pass_id)
    except DigiPass.DoesNotExist:
        logger.warning(f"[Webhook] DigiPass with pass_id {pass_id} does not exist. Skipping pass minted event.")
        return

    with transaction.atomic():
        if PassTransaction.objects.filter(tx_hash=tx_hash).exists():
            return

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
        if profile.referred_by and not profile.has_pass:
            from main.utils import award_referral_points
            award_referral_points(profile)

        # Login points adjustment logic
        from datetime import date
        today = date.today()
        if profile.last_login_date == today:
            old_pass = profile.current_pass
            old_power = getattr(old_pass, "point_power", 1) if old_pass else 1
            new_power = getattr(digipass, "point_power", 1)
            if new_power > old_power:
                points_to_add = (new_power - old_power) * 10
                profile.scored_point += points_to_add
                logger.info(f"[Webhook] Adjusted daily login points for {wallet}: +{points_to_add} points (power {old_power} -> {new_power})")

        profile.current_pass = digipass
        profile.has_pass = True
        profile.save(update_fields=["current_pass", "has_pass", "scored_point"])


def handle_pass_upgraded(event):
    tx_hash = event["transactionHash"]
    args = event["decodedEvent"]["params"]

    wallet = args["user"]["value"]
    new_pass_id = int(args["newPassId"]["value"])
    amount_paid = Decimal(args["amountPaid"]["value"]) / Decimal(10**18)

    try:
        user = DigiUser.objects.get(wallet_address__iexact=wallet)
    except DigiUser.DoesNotExist:
        logger.warning(f"[Webhook] DigiUser with wallet {wallet} does not exist. Skipping pass upgraded event.")
        return

    try:
        new_pass = DigiPass.objects.get(pass_id=new_pass_id)
    except DigiPass.DoesNotExist:
        logger.warning(f"[Webhook] DigiPass with pass_id {new_pass_id} does not exist. Skipping pass upgraded event.")
        return

    with transaction.atomic():
        if PassTransaction.objects.filter(tx_hash=tx_hash).exists():
            return

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

        # Login points adjustment logic
        from datetime import date
        today = date.today()
        if profile.last_login_date == today:
            old_pass = profile.current_pass
            old_power = getattr(old_pass, "point_power", 1) if old_pass else 1
            new_power = getattr(new_pass, "point_power", 1)
            if new_power > old_power:
                points_to_add = (new_power - old_power) * 10
                profile.scored_point += points_to_add
                logger.info(f"[Webhook] Adjusted daily login points for {wallet} (upgrade): +{points_to_add} points (power {old_power} -> {new_power})")

        profile.current_pass = new_pass
        profile.save(update_fields=["current_pass", "scored_point"])


