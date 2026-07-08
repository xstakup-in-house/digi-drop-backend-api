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
        from django.db import models
        from django.utils import timezone
        from datetime import date, timedelta
        
        today = timezone.now().date()
        user = self.request.user

        # Fetch all tasks that are active and fit within the scheduled date range
        all_active_tasks = Task.objects.filter(is_active=True).filter(
            models.Q(start_date__isnull=True) | models.Q(start_date__lte=today)
        ).filter(
            models.Q(end_date__isnull=True) | models.Q(end_date__gte=today)
        )

        visible_tasks = []
        for task in all_active_tasks:
            # Check completions for this task
            completions = UserTaskCompletion.objects.filter(
                user=user, 
                task=task, 
                status=UserTaskCompletion.Status.COMPLETED
            )

            if task.reset_interval == 'one_time':
                # If they have completed it once, hide it
                if not completions.exists():
                    visible_tasks.append(task.id)
            elif task.reset_interval == 'daily':
                # If they completed it today, hide it. Otherwise show it.
                if not completions.filter(completed_at__date=today).exists():
                    visible_tasks.append(task.id)
            elif task.reset_interval == 'weekly':
                # If they completed it in the last 7 days, hide it. Otherwise show it.
                seven_days_ago = timezone.now() - timedelta(days=7)
                if not completions.filter(completed_at__gte=seven_days_ago).exists():
                    visible_tasks.append(task.id)

        return Task.objects.filter(id__in=visible_tasks)

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["request"] = self.request
        return context


class StartTaskView(views.APIView):
    permission_classes = [HasPassPermission]

    def post(self, request, task_id):
        from django.utils import timezone
        today = timezone.now().date()
        
        task = get_object_or_404(Task, id=task_id, is_active=True)
        
        # Verify task is currently active based on start_date and end_date
        if task.start_date and task.start_date > today:
            return response.Response({"error": "Task is not active yet."}, status=400)
        if task.end_date and task.end_date < today:
            return response.Response({"error": "Task has expired."}, status=400)

        # Get or create completion record. For daily/weekly tasks, we allow starting a new record if the previous ones are completed.
        # Find if there is an active (started but not completed) completion
        user_task = UserTaskCompletion.objects.filter(
            user=request.user,
            task=task,
            status=UserTaskCompletion.Status.STARTED
        ).first()

        if not user_task:
            # Check if one_time task is already completed
            completed_exists = UserTaskCompletion.objects.filter(
                user=request.user,
                task=task,
                status=UserTaskCompletion.Status.COMPLETED
            ).exists()

            if task.reset_interval == 'one_time' and completed_exists:
                return response.Response({"error": "Task already completed"}, status=400)
            
            # For daily/weekly, check if completed within the limit
            if task.reset_interval == 'daily' and UserTaskCompletion.objects.filter(user=request.user, task=task, status=UserTaskCompletion.Status.COMPLETED, completed_at__date=today).exists():
                return response.Response({"error": "Task already completed today"}, status=400)

            # Create a fresh completion record
            user_task = UserTaskCompletion.objects.create(
                user=request.user,
                task=task,
                status=UserTaskCompletion.Status.STARTED,
                started_at=timezone.now()
            )
        else:
            # Re-save status to confirm started state
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
        # Retrieve the currently started task completion for this user
        user_task = UserTaskCompletion.objects.filter(
            user=request.user, 
            task_id=task_id, 
            status=UserTaskCompletion.Status.STARTED
        ).first()

        if not user_task:
            return response.Response({"error": "Task not started"}, status=400)

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
            w3_bsc = Web3(Web3.HTTPProvider(settings.BSC_RPC))
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
                "from": "Digidrops Vanguard <pilot@digidrops.xyz>",
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
        faucet_msg = f"We have dispatched 0.04 tBNB to your wallet address ({wallet_address}) to cover your passport minting fees. Tx Hash: {faucet_tx_hash or 'Pending'}"
        if faucet_error:
            faucet_msg = f"We encountered a temporary network delay dispatching your testnet BNB. Our support pilot will credit your wallet address ({wallet_address}) shortly."
            
        email1_body = f"""
        <h2>Welcome to the Vanguard, Pioneer!</h2>
        <p>Your testnet application has been received successfully.</p>
        <p>{faucet_msg}</p>
        <p>Prepare to explore the future of decentralized intelligence.</p>
        {footer_html}
        """
        send_email("Welcome to the Digidrops Testnet!", email1_body)

        # Email 2: Follow-up (T+48h)
        email2_body = f"""
        <h2>Day 2 Vanguard Report</h2>
        <p>Hope you've successfully touched down and tested the flight deck systems!</p>
        <p>If you haven't minted your Passport yet, please head over to <a href="https://digidrops.xyz">digidrops.xyz</a> to begin your missions.</p>
        {footer_html}
        """
        send_email("Continuing Your Flight Deck Journey", email2_body, delay_hours=48)

        # Email 3: Week-1 Progress & Feedback (T+7d)
        email3_body = f"""
        <h2>Week 1 Check-In: Requesting Pilot Feedback</h2>
        <p>You've been in orbit for a week! We'd love to hear your feedback on the onboarding experience, UI responsiveness, or the passport system.</p>
        <p>Please reply to this email or contact the Chief Pilot directly at <a href="mailto:chiefpilot@digidrops.xyz">chiefpilot@digidrops.xyz</a> if you need anything.</p>
        {footer_html}
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
