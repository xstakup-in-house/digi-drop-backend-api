from django.shortcuts import get_object_or_404
from django.http import JsonResponse, HttpResponseForbidden
from django.utils import timezone
from datetime import date
from django.conf import settings
from web3 import Web3
import json
import hmac
import hashlib
import logging
from .utils import get_bnb_usd_price
from main.services.pass_verifier import (handle_pass_minted,handle_pass_upgraded)
from django.views.decorators.csrf import csrf_exempt
from django.db import transaction, IntegrityError
from django.db.models import F, Window, Max
from django.db.models.functions import Rank
from eth_account.messages import encode_defunct
from django.utils.crypto import get_random_string
from decimal import Decimal
from .permissions import HasPassPermission
from rest_framework import generics, response, permissions, status, views
from .serializers import  DigiPassSerializer, LeaderboardSerializer, UpdateProfileSerializer, UserProfileSerializer, TaskSerializer, UserTaskCompletionSerializer
from .models import DigiUser, DigiPass,LoginNonce, PassTransaction,Profile, Task, UserTaskCompletion
from rest_framework_simplejwt.tokens import RefreshToken

logger = logging.getLogger(__name__)



BSC_RPC = settings.BSC_RPC  
web3 = Web3(Web3.HTTPProvider(BSC_RPC))



class WalletLoginView(views.APIView):
    def get(self, request):
        # Generate nonce for signing (valid for 5 minutes)
        nonce = get_random_string(32)
        LoginNonce.objects.create(nonce=nonce)
        message = f"Login to Digidrop: {nonce}"
        return response.Response({'nonce': nonce, 'message': message}, status=status.HTTP_200_OK)
    
    def post(self, request):  
        wallet_address = request.data.get('walletAddress', '').strip().lower()
        signature = request.data.get('signature')
        nonce_str = request.data.get('nonce')
        
        user_ref = request.data.get('referral')  # From frontend

        if not all([wallet_address, signature, nonce_str]):
            return response.Response({'error': 'Missing fields'}, status=400)
        
        try:
            nonce_obj = LoginNonce.objects.get(nonce=nonce_str)
            if nonce_obj.used or nonce_obj.is_expired():
                return response.Response({'error': 'Invalid or expired nonce'}, status=400)
        except LoginNonce.DoesNotExist:
            return response.Response({'error': 'Invalid nonce'}, status=400)

        # Verify signature
        message = f"Login to Digidrop: {nonce_str}"

        # Step 1: Validate address format
        w3 = Web3()
        if not w3.is_checksum_address(wallet_address):
            try:
                wallet_address = w3.to_checksum_address(wallet_address)
            except ValueError:
                return response.Response({'error': 'Invalid wallet address'}, status=status.HTTP_400_BAD_REQUEST)

        # Step 2: Verify signature
        try:
            encoded_msg = encode_defunct(text=message)
            recovered_address = w3.eth.account.recover_message(encoded_msg, signature=signature)
            if recovered_address.lower() != wallet_address.lower():
                return response.Response({'error': 'Signature mismatch'}, status=status.HTTP_400_BAD_REQUEST)
        
        except Exception:
            return response.Response({'error': 'Invalid signature'}, status=status.HTTP_400_BAD_REQUEST)

        nonce_obj.used = True
        nonce_obj.save()
        # Create/find user
        user, created = DigiUser.objects.get_or_create(
            wallet_address__iexact=wallet_address,
            defaults={'wallet_address': wallet_address}
            )
        if created and user_ref:
            referrer = DigiUser.objects.filter(profile__referral_code=user_ref).first()
            if referrer:
                user.profile.referred_by = referrer
                user.profile.save()
        profile = user.profile  # Assuming OneToOneField; create if not exists via signal
        today = date.today()
        if profile.last_login_date != today:
            base_login_points = 10 
            multiplier = getattr(profile.current_pass, "point_power", 1)
            profile.scored_point += base_login_points * multiplier
            profile.last_login_date = today
            profile.save()
        refresh = RefreshToken.for_user(user)
        return response.Response({'token': str(refresh.access_token),'refresh': str(refresh),'isNewUser': created}, status=status.HTTP_200_OK)

class PassListEndpoint(generics.ListAPIView):
    serializer_class=DigiPassSerializer
    queryset=DigiPass.objects.all()

class PassDetailEndpoint(generics.RetrieveAPIView):
    serializer_class=DigiPassSerializer
    queryset=DigiPass.objects.all()
    lookup_field= "id"

class UpdateProfileEndpoint(generics.UpdateAPIView):
    serializer_class = UpdateProfileSerializer
    permission_classes = [HasPassPermission] 
    def get_object(self):
        """
        Always return the profile of the logged-in user
        """
        return self.request.user.profile

class UserProfileView(generics.RetrieveAPIView):
    serializer_class = UserProfileSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        profile = (
            Profile.objects.select_related("user", "current_pass").get(user=self.request.user)
        )

        # Layer 2 — Self-healing: DB says no pass, but maybe the webhook missed it.
        # Check the smart contract directly and fix the DB if needed.
        if not profile.has_pass:
            self._sync_pass_from_chain(profile)

        return profile

    def _sync_pass_from_chain(self, profile):
        """Query the BNB Chain contract and heal profile.has_pass if user already minted."""
        try:
            wallet = self.request.user.wallet_address
            w3 = Web3(Web3.HTTPProvider(settings.BSC_RPC))
            with open(settings.BASE_DIR / 'contracts' / 'abi.json') as f:
                abi = json.load(f)
            contract = w3.eth.contract(address=settings.CONTRACT_ADDRESS, abi=abi)

            pass_id, _ = contract.functions.getUserPass(wallet).call()

            if pass_id > 0:
                digipass = DigiPass.objects.filter(pass_id=pass_id).first()
                if digipass:
                    logger.info(
                        f"[SelfHeal] Healed profile for {wallet}: pass_id={pass_id} found on-chain but missing in DB."
                    )
                    profile.has_pass = True
                    profile.current_pass = digipass
                    profile.save(update_fields=["has_pass", "current_pass"])
        except Exception as exc:
            # Never crash the /profile endpoint over a chain call failure
            logger.warning(f"[SelfHeal] On-chain check failed for {self.request.user.wallet_address}: {exc}")
    
class UserProfileStatsView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        profile = request.user.profile
        points = profile.scored_point

        # Calculate rank: count profiles with higher scored_points + 1
        rank = Profile.objects.filter(scored_point__gt=points).count() + 1

        # 2️⃣ Get highest score on the platform (rank #1 points)
        highest_score = (
            Profile.objects.aggregate(max_score=Max("scored_point")).get("max_score", 0)
        )

        # 3️⃣ Referral count
        referral_count = request.user.referred_users.count()

        return response.Response({
            "point": points,
            "rank": rank,
            "highest_point": highest_score or 0,
            "referral_count": referral_count,
        }, status=status.HTTP_200_OK)


class VerifyPaymentView(generics.GenericAPIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        tx_hash = request.data['txHash']
        is_upgrade = request.data.get('isUpgrade', False)
        new_pass_id = request.data['newPassId']  

        bnb_usd = get_bnb_usd_price()

        w3 = Web3(Web3.HTTPProvider(settings.BSC_RPC))
        contract_address = settings.CONTRACT_ADDRESS
        with open(settings.BASE_DIR / 'contracts' / 'abi.json') as f:
            abi = json.load(f)
        contract = w3.eth.contract(address=contract_address, abi=abi)

        tx_receipt = w3.eth.get_transaction_receipt(tx_hash)
        if tx_receipt.status != 1:
            return response.Response({'error': 'Mint Transaction failed'}, status=status.HTTP_400_BAD_REQUEST)
        
        tx = w3.eth.get_transaction(tx_hash)
        if tx['to'].lower() != contract_address.lower():
            return response.Response({'error': 'Invalid contract address in tx'}, status=status.HTTP_400_BAD_REQUEST)
        
        pass_id, points = contract.functions.getUserPass(tx['from']).call()
        if pass_id != new_pass_id:
            return response.Response({'error': 'Minted pass ID mismatch'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            with transaction.atomic():
                existing = PassTransaction.objects.select_for_update().filter(
                    tx_hash=tx_hash
                ).first()

                if existing and existing.is_verified:
                    return response.Response({"success": True})

                digipass = DigiPass.objects.get(pass_id=new_pass_id)

                tx_obj = existing or PassTransaction(
                    tx_hash=tx_hash,
                    user=request.user,
                )

                tx_obj.wallet_address = tx["from"]
                tx_obj.digipass = digipass
                tx_obj.minted = True
                tx_obj.is_verified = True
                tx_obj.amount_paid_bnb = Decimal(
                    w3.from_wei(tx["value"], "ether")
                )
                tx_obj.usd_price = digipass.usd_price
                tx_obj.is_upgrade = is_upgrade
                tx_obj.save()

                profile = request.user.profile
                if profile.referred_by and not profile.has_pass:
                    from .utils import award_referral_points
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
                        logger.info(f"[VerifyPayment] Adjusted daily login points for {request.user.wallet_address}: +{points_to_add} points (power {old_power} -> {new_power})")

                profile.current_pass = digipass
                profile.has_pass = True
                profile.save(update_fields=["current_pass", "has_pass", "scored_point"])
        except IntegrityError:
            return response.Response({"success": True})

        return response.Response({
            "success": True,
            "pass_id": pass_id,
            "points": points,
            "tx_hash": tx_hash,
        })
            

def verify_signature(body, signature):
    secret = settings.MORALIS_WEBHOOK_SECRET.encode()
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    result = hmac.compare_digest(expected, signature)
    if not result:
        logger.error(
            f"[Webhook] Signature mismatch. "
            f"Expected={expected[:16]}... Received={str(signature)[:16]}..."
        )
    return result


@csrf_exempt
def moralis_webhook(request):  
    if "x-signature" not in request.headers:
        # Log so we can detect misconfigured Moralis streams in production
        logger.warning(
            "[Webhook] Received Moralis request with NO x-signature header. "
            "Check that your Moralis stream has a webhook secret configured."
        )
        return JsonResponse({"status": "ok"}, status=200)

    signature = request.headers.get("X-Signature")
    body = request.body

    if not signature or not verify_signature(body, signature):
        return HttpResponseForbidden("Invalid signature")

    payload = json.loads(body)

    # Moralis batches events
    for event in payload.get("logs", []):
        event_name = event["decodedEvent"]["label"]

        if event_name == "PassMinted":
            handle_pass_minted(event)

        elif event_name == "PassUpgraded":
            handle_pass_upgraded(event)

    return JsonResponse({"status": "ok"})
    
class TaskListView(generics.ListAPIView):
    permission_classes = [HasPassPermission]
    serializer_class = TaskSerializer

    def get_queryset(self):
        # Only incomplete, active tasks for user
        
        completed_tasks = UserTaskCompletion.objects.filter(
            user=self.request.user, status=UserTaskCompletion.Status.COMPLETED
        ).values_list('task_id', flat=True)
        return Task.objects.filter(is_active=True).exclude(id__in=completed_tasks)
    
    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["request"] = self.request
        return context
    

class StartTaskView(views.APIView):
    permission_classes = [HasPassPermission]

    def post(self, request, task_id):
        task = get_object_or_404(Task, id=task_id, is_active=True)

        user_task, created = UserTaskCompletion.objects.get_or_create(
            user=request.user,
            task=task
        )

        if user_task.status == UserTaskCompletion.Status.COMPLETED:
            return response.Response(
                {"error": "Task already completed"},
                status=400
            )

        user_task.status = UserTaskCompletion.Status.STARTED
        user_task.started_at = timezone.now()
        user_task.save()

        return response.Response({
            "task_type": task.task_type,
            "external_link": task.external_link
        }, status=status.HTTP_200_OK)

   

class CompleteTaskView(views.APIView):
    permission_classes = [HasPassPermission]

    def post(self, request, task_id):
        user_task = get_object_or_404(UserTaskCompletion, user=request.user, task_id=task_id)

        if user_task.status != UserTaskCompletion.Status.STARTED:
            return response.Response({"error": "Task not started"},status=400)

        # Award points
        profile = request.user.profile
        multiplier = getattr(profile.current_pass, "point_power", 1)
        multiplied_points = user_task.task.points * multiplier
        profile.scored_point += multiplied_points
        profile.save(update_fields=["scored_point"])

        user_task.status = UserTaskCompletion.Status.COMPLETED
        user_task.completed_at = timezone.now()
        user_task.awarded_points = multiplied_points
        user_task.save()

        return response.Response({
            "success": True,
            "points_awarded": multiplied_points
        })

class LeaderboardView(generics.ListAPIView):
    permission_classes = [HasPassPermission]  # Public leaderboard
    serializer_class = LeaderboardSerializer
    pagination_class = None  # No pagination needed for top 100

    def get_queryset(self):
        return (
        Profile.objects.filter(has_pass=True, current_pass__isnull=False)
        .order_by('-scored_point')[:100]
    )

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        # Add rank to each (1-based)
        data = []
        for rank, profile in enumerate(queryset, start=1):
            serializer = self.get_serializer(profile, context={'rank': rank})
            data.append(serializer.data)
        return response.Response(data, status=status.HTTP_200_OK)


class TestnetOnboardView(views.APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        import os
        import requests
        from datetime import timedelta
        from django.core.cache import cache
        from django.db.models import Q
        from .models import TestnetApplication

        wallet_address = request.data.get('walletAddress', '').strip().lower()
        email = request.data.get('email', '').strip().lower()

        if not wallet_address or not email:
            return response.Response({'error': 'Wallet address and email are required.'}, status=status.HTTP_400_BAD_REQUEST)

        # Step 1: Validate wallet format
        w3 = Web3()
        if not w3.is_checksum_address(wallet_address):
            try:
                wallet_address = w3.to_checksum_address(wallet_address)
            except ValueError:
                return response.Response({'error': 'Invalid wallet address format.'}, status=status.HTTP_400_BAD_REQUEST)

        # Step 2: Rate Limiting & Uniqueness Checks
        if TestnetApplication.objects.filter(wallet_address__iexact=wallet_address).exists():
            return response.Response({'error': 'This wallet address has already been registered.'}, status=status.HTTP_400_BAD_REQUEST)

        if TestnetApplication.objects.filter(email__iexact=email).exists():
            return response.Response({'error': 'This email address has already been registered.'}, status=status.HTTP_400_BAD_REQUEST)

        client_ip = request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR', '')).split(',')[0].strip()
        ip_cache_key = f"testnet_ip_{client_ip}"
        if cache.get(ip_cache_key):
            return response.Response({'error': 'Too many requests from this IP. Please wait 7 days.'}, status=status.HTTP_429_TOO_MANY_REQUESTS)

        # Step 3: Programmatic Faucet Dispatch
        faucet_tx_hash = None
        faucet_error = None
        try:
            w3_bsc = Web3(Web3.HTTPProvider(getattr(settings, 'BSC_RPC_DEV_URL', os.getenv('BSC_RPC_DEV_URL'))))
            account = w3_bsc.eth.account.from_key(settings.PRIVATE_KEY)
            nonce = w3_bsc.eth.get_transaction_count(account.address, 'pending')
            gas_price = w3_bsc.eth.gas_price
            
            tx = {
                'nonce': nonce,
                'to': wallet_address,
                'value': w3_bsc.to_wei(0.04, 'ether'),
                'gas': 21000,
                'gasPrice': gas_price,
                'chainId': 97
            }
            signed_tx = account.sign_transaction(tx)
            tx_hash = w3_bsc.eth.send_raw_transaction(signed_tx.raw_transaction)
            faucet_tx_hash = w3_bsc.to_hex(tx_hash)
        except Exception as e:
            faucet_error = str(e)
            logger.error(f"Faucet transfer failed to {wallet_address}: {e}")

        # Step 4: Save Application
        app = TestnetApplication.objects.create(
            community_name=request.data.get('communityName', ''),
            platform=request.data.get('platform', ''),
            invite_link=request.data.get('inviteLink', ''),
            member_count=request.data.get('memberCount', ''),
            wallet_address=wallet_address,
            email=email,
            feedback=request.data.get('feedback', ''),
            faucet_tx_hash=faucet_tx_hash
        )

        # Set IP cache rate limit for 7 days
        cache.set(ip_cache_key, True, timeout=7 * 24 * 60 * 60)

        # Step 5: Send Emails via Resend
        resend_key = getattr(settings, 'RESEND_API_KEY', os.getenv('RESEND_API_KEY', 're_123456789'))
        
        def send_email(subject, html_body, delay_hours=None):
            headers = {
                "Authorization": f"Bearer {resend_key}",
                "Content-Type": "application/json"
            }
            payload = {
                "from": f"Digidrops Vanguard <{settings.DEFAULT_FROM_EMAIL}>",
                "to": [email],
                "subject": subject,
                "html": html_body
            }
            if delay_hours:
                scheduled_time = timezone.now() + timedelta(hours=delay_hours)
                payload["scheduled_at"] = scheduled_time.isoformat()
            
            try:
                requests.post("https://api.resend.com/emails", json=payload, headers=headers)
            except Exception as ex:
                logger.error(f"Failed to queue email to {email}: {ex}")

        # Footer template with socials
        footer_html = """
        <br><br>
        <hr style="border:0; border-top: 1px solid #eee; margin: 20px 0;">
        <p style="font-size:12px; color:#666;">
          Follow the Vanguard journey:<br>
          X/Twitter: <a href="https://x.com/Digidrops_xyz">@Digidrops_xyz</a> | 
          Telegram: <a href="https://t.me/DigidropsAI">DigidropsAI</a> | 
          Discord: <a href="https://discord.com/invite/digidropsai">Join Discord</a>
        </p>
        """

        # Email 1: Welcome & Faucet Info
        faucet_msg = f"We have successfully dispatched 0.04 tBNB to your wallet address ({wallet_address}) to cover your passport minting fees. Tx Hash: {faucet_tx_hash or 'Pending'}"
        if faucet_error:
            faucet_msg = f"We encountered a temporary network delay dispatching your testnet BNB. Our support pilot will credit your wallet address ({wallet_address}) shortly."

        # Email 1: Welcome (Immediate)
        email1_body = f"""
        <!DOCTYPE html>
        <html>
        <head>
          <meta charset='utf-8'>
          <meta name='viewport' content='width=device-width, initial-scale=1.0'>
          <title>Welcome to the Vanguard, Pioneer</title>
          <style>
            body {{ margin: 0; padding: 0; background-color: #050510; color: #cbd5e1; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }}
            .email-container {{ max-width: 600px; margin: 0 auto; padding: 40px 20px; background-color: #050510; }}
            .brand-card {{
              background: linear-gradient(135deg, rgba(30, 27, 75, 0.4) 0%, rgba(15, 23, 42, 0.6) 100%);
              border: 1px solid rgba(96, 165, 250, 0.15); border-radius: 24px; padding: 40px 30px; text-align: center;
            }}
            .badge {{
              display: inline-flex; align-items: center; gap: 8px; padding: 6px 16px; border-radius: 100px;
              background-color: rgba(96, 165, 250, 0.08); border: 1px solid rgba(96, 165, 250, 0.2);
              color: #60A5FA; font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.2em; margin-bottom: 24px;
            }}
            .glow-dot {{ width: 5px; height: 5px; background-color: #60A5FA; border-radius: 50%; box-shadow: 0 0 8px #60A5FA; }}
            .hero-title {{ font-size: 32px; font-weight: 700; line-height: 1.15; margin: 0 0 20px 0; color: #60A5FA; }}
            .body-text {{ font-size: 15px; line-height: 1.6; color: #94a3b8; margin: 0 0 30px 0; }}
            .highlight-box {{ background: rgba(96, 165, 250, 0.03); border: 1px dashed rgba(96, 165, 250, 0.25); border-radius: 16px; padding: 24px; margin: 30px 0; text-align: left; }}
            .highlight-title {{ color: #60A5FA; font-size: 12px; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase; margin: 0 0 10px 0; }}
            .highlight-body {{ font-size: 13.5px; line-height: 1.5; color: #cbd5e1; margin: 0; }}
            .action-button {{
              display: inline-block; background: linear-gradient(135deg, #60A5FA 0%, #7C3AED 100%);
              border-radius: 14px; padding: 16px 36px; color: #ffffff !important; font-size: 14px; font-weight: 700; text-decoration: none;
              margin: 15px 0 35px 0; border: 1px solid rgba(255,255,255,0.1);
            }}
            .divider {{ border: none; border-top: 1px solid rgba(255, 255, 255, 0.06); margin: 35px 0 25px 0; }}
            .footer-text {{ font-size: 12px; color: #475569; margin: 0; line-height: 1.5; }}
            .footer-links a {{ color: #60A5FA; text-decoration: none; font-size: 11px; margin: 0 8px; }}
          </style>
        </head>
        <body>
          <div class='email-container'>
            <div class='brand-card'>
              <div class='badge'><span class='glow-dot'></span>Signal Logged</div>
              <h1 class='hero-title'>Welcome to the Vanguard.</h1>
              <p class='body-text'>
                Greetings, Pioneer. We have successfully captured the subspace <strong>signal</strong> of the <strong>{request.data.get('communityName', 'Vanguard')}</strong> fleet. Your coordinates have been successfully logged on our navigation grid.
              </p>
              <div class='highlight-box'>
                <div class='highlight-title'>Faucet Refuel Dispatch</div>
                <p class='highlight-body'>{faucet_msg}</p>
              </div>
              <a href='https://www.digidrops.xyz/login' class='action-button'>Mint Passport & Start Now</a>
              <p class='body-text' style='font-size: 14px; margin-bottom: 0;'>
                Prepare to explore the future of decentralized intelligence. Keep your scanners active.
              </p>
              <hr class='divider' />
              <p class='footer-text' style='margin-bottom: 12px; font-weight: 500;'>Safe travels through the stars,</p>
              <p class='footer-text' style='color: #60A5FA; font-size: 14px; font-weight: bold; margin-bottom: 25px; letter-spacing: 0.05em;'>THE DIGIDROPS TEAM</p>
              <div class='footer-links'>
                <a href='https://www.digidrops.xyz/privacy-policy'>PRIVACY POLICY</a>
                <span style='color: #1e293b;'>•</span>
                <a href='https://www.digidrops.xyz/term-and-condition'>TERMS OF MISSION</a>
              </div>
            </div>
          </div>
        </body>
        </html>
        """
        send_email("Welcome to the Digidrops Testnet!", email1_body)

        # Email 2: Day 2 Follow-up (T+48h)
        email2_body = f"""
        <!DOCTYPE html>
        <html>
        <head>
          <meta charset='utf-8'>
          <meta name='viewport' content='width=device-width, initial-scale=1.0'>
          <title>Continuing Your Flight Deck Journey</title>
          <style>
            body {{ margin: 0; padding: 0; background-color: #050510; color: #cbd5e1; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }}
            .email-container {{ max-width: 600px; margin: 0 auto; padding: 40px 20px; background-color: #050510; }}
            .brand-card {{
              background: linear-gradient(135deg, rgba(46, 16, 101, 0.3) 0%, rgba(15, 23, 42, 0.7) 100%);
              border: 1px solid rgba(167, 139, 250, 0.2); border-radius: 24px; padding: 40px 30px; text-align: center;
            }}
            .badge {{
              display: inline-flex; align-items: center; gap: 8px; padding: 6px 16px; border-radius: 100px;
              background-color: rgba(167, 139, 250, 0.08); border: 1px solid rgba(167, 139, 250, 0.2);
              color: #A78BFA; font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.2em; margin-bottom: 24px;
            }}
            .glow-dot {{ width: 5px; height: 5px; background-color: #A78BFA; border-radius: 50%; box-shadow: 0 0 8px #A78BFA; }}
            .hero-title {{ font-size: 32px; font-weight: 700; line-height: 1.15; margin: 0 0 20px 0; color: #A78BFA; }}
            .body-text {{ font-size: 15px; line-height: 1.6; color: #94a3b8; margin: 0 0 30px 0; }}
            .action-button {{
              display: inline-block; background: linear-gradient(135deg, #60A5FA 0%, #7C3AED 100%);
              border-radius: 14px; padding: 16px 36px; color: #ffffff !important; font-size: 14px; font-weight: 700; text-decoration: none;
              margin: 15px 0 35px 0; border: 1px solid rgba(255,255,255,0.1);
            }}
            .support-card {{ background: rgba(255, 255, 255, 0.01); border: 1px solid rgba(255, 255, 255, 0.05); border-radius: 16px; padding: 20px; text-align: left; }}
            .support-title {{ font-size: 11px; font-weight: bold; color: #60A5FA; text-transform: uppercase; margin-bottom: 6px; }}
            .support-text {{ font-size: 12.5px; line-height: 1.5; color: #94a3b8; margin: 0; }}
            .support-link {{ color: #60A5FA; text-decoration: none; font-weight: 600; }}
            .divider {{ border: none; border-top: 1px solid rgba(255, 255, 255, 0.06); margin: 35px 0 25px 0; }}
            .footer-text {{ font-size: 12px; color: #475569; margin: 0; line-height: 1.5; }}
            .footer-links a {{ color: #A78BFA; text-decoration: none; font-size: 11px; margin: 0 8px; }}
          </style>
        </head>
        <body>
          <div class='email-container'>
            <div class='brand-card'>
              <div class='badge'><span class='glow-dot'></span>Flight Update</div>
              <h1 class='hero-title'>Your Vanguard Progress Report</h1>
              <p class='body-text'>
                Pioneer, how has your journey into the Digiverse been so far? We are watching the telemetry coordinates, and your presence is crucial as we lay down the foundations of the Human Data Layer.
              </p>
              <p class='body-text'>
                Are you actively completing missions on the flight deck and climbing the leaderboard to secure your rank? Your contributions are what make this ecosystem bot-free and resilient.
              </p>
              <div class='support-card'>
                <div class='support-title'>✦ We Value Your Signal</div>
                <p class='support-text'>
                  If you have any initial thoughts, issues, or suggestions, reply directly to this telemetry feed or contact the Chief Pilot. We shape this flight deck together.
                </p>
              </div>
              <p class='body-text' style='font-size: 14.5px;'>
                <strong>Crucial Action Required:</strong> If you haven't minted your Soulbound Passport yet, the time is now. Mint your pass to unlock your Stardust multiplier and officially join the ranks.
              </p>
              <a href='https://www.digidrops.xyz/login' class='action-button'>Mint Passport & Start Now</a>
              <hr class='divider' />
              <p class='footer-text' style='margin-bottom: 12px; font-weight: 500;'>See you at the horizon,</p>
              <p class='footer-text' style='color: #A78BFA; font-size: 14px; font-weight: bold; margin-bottom: 25px; letter-spacing: 0.05em;'>THE DIGIDROPS TEAM</p>
              <div class='footer-links'>
                <a href='https://www.digidrops.xyz/privacy-policy'>PRIVACY POLICY</a>
                <span style='color: #1e293b;'>•</span>
                <a href='https://www.digidrops.xyz/term-and-condition'>TERMS OF MISSION</a>
              </div>
            </div>
          </div>
        </body>
        </html>
        """
        send_email("Continuing Your Flight Deck Journey", email2_body, delay_hours=48)

        # Email 3: Week-1 Progress & Feedback (T+7d)
        email3_body = f"""
        <!DOCTYPE html>
        <html>
        <head>
          <meta charset='utf-8'>
          <meta name='viewport' content='width=device-width, initial-scale=1.0'>
          <title>Vanguard Week-1 Check-In & Feedback</title>
          <style>
            body {{ margin: 0; padding: 0; background-color: #050510; color: #cbd5e1; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }}
            .email-container {{ max-width: 600px; margin: 0 auto; padding: 40px 20px; background-color: #050510; }}
            .brand-card {{
              background: linear-gradient(135deg, rgba(30, 27, 75, 0.4) 0%, rgba(15, 23, 42, 0.6) 100%);
              border: 1px solid rgba(96, 165, 250, 0.15); border-radius: 24px; padding: 40px 30px; text-align: center;
            }}
            .badge {{
              display: inline-flex; align-items: center; gap: 8px; padding: 6px 16px; border-radius: 100px;
              background-color: rgba(96, 165, 250, 0.08); border: 1px solid rgba(96, 165, 250, 0.2);
              color: #60A5FA; font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.2em; margin-bottom: 24px;
            }}
            .glow-dot {{ width: 5px; height: 5px; background-color: #60A5FA; border-radius: 50%; box-shadow: 0 0 8px #60A5FA; }}
            .hero-title {{ font-size: 32px; font-weight: 700; line-height: 1.15; margin: 0 0 20px 0; color: #60A5FA; }}
            .body-text {{ font-size: 15px; line-height: 1.6; color: #94a3b8; margin: 0 0 30px 0; }}
            .support-card {{ background: rgba(255, 255, 255, 0.01); border: 1px solid rgba(255, 255, 255, 0.05); border-radius: 16px; padding: 20px; text-align: left; }}
            .support-title {{ font-size: 11px; font-weight: bold; color: #60A5FA; text-transform: uppercase; margin-bottom: 6px; }}
            .support-text {{ font-size: 12.5px; line-height: 1.5; color: #94a3b8; margin: 0; }}
            .support-link {{ color: #60A5FA; text-decoration: none; font-weight: 600; }}
            .divider {{ border: none; border-top: 1px solid rgba(255, 255, 255, 0.06); margin: 35px 0 25px 0; }}
            .footer-text {{ font-size: 12px; color: #475569; margin: 0; line-height: 1.5; }}
            .footer-links a {{ color: #60A5FA; text-decoration: none; font-size: 11px; margin: 0 8px; }}
          </style>
        </head>
        <body>
          <div class='email-container'>
            <div class='brand-card'>
              <div class='badge'><span class='glow-dot'></span>Feedback Request</div>
              <h1 class='hero-title'>Vanguard Week-1 Check-In</h1>
              <p class='body-text'>
                You've been in orbit for a week! We'd love to hear your feedback on the onboarding experience, UI responsiveness, or the passport system.
              </p>
              <div class='support-card'>
                <div class='support-title'>✦ Chief Pilot Direct Support</div>
                <p class='support-text'>
                  Please reply directly to this email or contact the Chief Pilot at <span class='support-link'>digidrops@proton.me</span> to share your feedback or ask any questions.
                </p>
              </div>
              <hr class='divider' />
              <p class='footer-text' style='margin-bottom: 12px; font-weight: 500;'>See you at the horizon,</p>
              <p class='footer-text' style='color: #60A5FA; font-size: 14px; font-weight: bold; margin-bottom: 25px; letter-spacing: 0.05em;'>THE DIGIDROPS TEAM</p>
              <div class='footer-links'>
                <a href='https://www.digidrops.xyz/privacy-policy'>PRIVACY POLICY</a>
                <span style='color: #1e293b;'>•</span>
                <a href='https://www.digidrops.xyz/term-and-condition'>TERMS OF MISSION</a>
              </div>
            </div>
          </div>
        </body>
        </html>
        """
        send_email("Vanguard Week-1 Check-In & Feedback", email3_body, delay_hours=168)

        return response.Response({
            'success': True,
            'faucet_tx_hash': faucet_tx_hash,
            'message': 'Onboarding coordinates recorded successfully.'
        }, status=status.HTTP_201_CREATED)


class GlobalStatsView(views.APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        from django.db.models import Sum
        from django.utils import timezone
        from datetime import datetime, time
        total_users = DigiUser.objects.count()
        total_passes = Profile.objects.filter(has_pass=True).count()
        total_points = Profile.objects.aggregate(sum_points=Sum('scored_point'))['sum_points'] or 0
        
        today_start = timezone.make_aware(datetime.combine(timezone.now().date(), time.min))
        minted_today = PassTransaction.objects.filter(minted=True, created_at__gte=today_start).count()

        return response.Response({
            'total_users': total_users,
            'total_passes': total_passes,
            'total_points': total_points,
            'minted_today': minted_today
        }, status=200)
